"""
This file implements the Job class for pegasus-monitord.
"""

##
#  Copyright 2007-2012 University Of Southern California
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
##

# Import Python modules
import os
import sys
import re
import logging

from Pegasus.tools import utils

logger = logging.getLogger(__name__)

# Global variables
good_rsl = {"maxcputime": 1, "maxtime":1, "maxwalltime": 1}
MAX_OUTPUT_LENGTH = 2**16-1  # Only keep stdout to 64K

# Used in parse_sub_file
re_rsl_string = re.compile(r"^\s*globusrsl\W", re.IGNORECASE)
re_rsl_clean = re.compile(r"([-_])")
re_site_parse_gvds = re.compile(r"^\s*\+(pegasus|wf)_(site|resource)\s*=\s*([\'\"])?(\S+)\3")
re_parse_transformation = re.compile(r"^\s*\+pegasus_wf_xformation\s*=\s*(\S+)")
re_parse_derivation = re.compile(r"^\s*\+pegasus_wf_dax_job_id\s*=\s*(\S+)")
re_parse_multiplier_factor = re.compile(r"^\s*\+pegasus_cores\s=\s(\S+)")
re_parse_executable = re.compile(r"^\s*executable\s*=\s*(\S+)")
re_parse_arguments = re.compile(r'^\s*arguments\s*=\s*"([^"\r\n]*)"')
re_parse_environment = re.compile(r'^\s*environment\s*=\s*(.*)')
re_site_parse_euryale = re.compile(r"^\#!\s+site=(\S+)")
re_parse_property = re.compile(r'([^:= \t]+)\s*[:=]?\s*(.*)')
re_parse_input = re.compile(r"^\s*intput\s*=\s*(\S+)")
re_parse_output = re.compile(r"^\s*output\s*=\s*(\S+)")
re_parse_error = re.compile(r"^\s*error\s*=\s*(\S+)")

class Job:
    """
    Class used to keep information needed to track a particular job
    """

    # Variables that describe a job, as per the Stampede schema
    # Some will be initialized in the init method, others will
    # get their values from the kickstart output file when a job
    # finished

    def __init__(self, wf_uuid, name, job_submit_seq):
        """
        This function initializes the job parameters with the
        information available when a job is detected in the
        "PRE_SCRIPT_STARTED" or the "SUBMIT" state. Other parameters
        will remain None until a job finishes and a kickstart output
        file can be parsed.
        """
        self._wf_uuid = wf_uuid
        self._exec_job_id = name
        self._job_submit_seq = job_submit_seq
        self._sched_id = None
        self._site_name = None
        self._host_id = None
        self._remote_user = None
        self._remote_working_dir = None
        self._cluster_start_time = None
        self._cluster_duration = None
        self._job_state = None
        self._job_state_seq = 0
        self._job_state_timestamp = None
        self._job_output_counter = 0
        self._pre_script_start = None
        self._pre_script_done = None
        self._pre_script_exitcode = None
        self._main_job_start = None
        self._main_job_done = None
        self._main_job_transformation = None
        self._main_job_derivation = None
        self._main_job_executable = None
        self._main_job_arguments = None
        self._main_job_exitcode = None
        self._main_job_multiplier_factor = None
        self._post_script_start = None
        self._post_script_done = None
        self._post_script_exitcode = None
        self._input_file = None
        self._output_file = None
        self._error_file = None
        self._stdout_text = None
        self._stderr_text = None
        self._job_dagman_out = None    # _CONDOR_DAGMAN_LOG from environment
                                       # line for pegasus-plan and subdax_ jobs
        self._kickstart_parsed = False # Flag indicating if the kickstart
                                       # output for this job was parsed or not
        self._has_rotated_stdout_err_files = False #Flag indicating whether we detected that job stdout|stderr
                                                  #was rotated or not, as is the default case.
        self._deferred_job_end_kwargs = None

    def set_job_state(self, job_state, sched_id, timestamp, status):
        """
        This function sets the job state for this job. It also updates
        the times the main job and PRE/POST scripts start and finish.
        """
        self._job_state = job_state
        self._job_state_timestamp = int(timestamp)
        # Increment job state sequence
        self._job_state_seq = self._job_state_seq + 1

        # Set sched_id if we don't already have it...
        if self._sched_id is None:
            self._sched_id = sched_id
        # Record timestamp for certain job states
        if job_state == "PRE_SCRIPT_STARTED":
            self._pre_script_start = int(timestamp)
        elif (job_state == "PRE_SCRIPT_SUCCESS" or
              job_state == "PRE_SCRIPT_FAILURE"):
            self._pre_script_done = int(timestamp)
            self._pre_script_exitcode = utils.regular_to_raw(status)
        elif job_state == "POST_SCRIPT_STARTED":
            self._post_script_start = int(timestamp)
        elif job_state == "POST_SCRIPT_TERMINATED":
            self._post_script_done = int(timestamp)
        elif job_state == "EXECUTE":
            self._main_job_start = int(timestamp)
        elif job_state == "JOB_TERMINATED":
            self._main_job_done = int(timestamp)
        elif job_state == "JOB_ABORTED":
            self._main_job_done = int(timestamp) # PM-805 job was aborted, good chance job terminated event did not happen.
        elif job_state == "JOB_SUCCESS" or job_state == "JOB_FAILURE":
            self._main_job_exitcode = utils.regular_to_raw(status)
        elif (job_state == "POST_SCRIPT_SUCCESS" or
              job_state == "POST_SCRIPT_FAILURE"):
            self._post_script_exitcode = utils.regular_to_raw(status)

    def parse_sub_file(self, stamp, submit_file):
        """
        This function parses a job's submit file and returns job
        planning information. In addition, we try to populate the job
        type from information in the submit file.
        # paramtr: stamp(IN): timestamp associated with the log line
        # paramtr: submit_file(IN): submit file name
        # globals: good_rsl(IN): which RSL keys constitute time requirements
        # returns: (largest job time requirement in minutes, destination site)
        # returns: (None, None) if sub file not found
        """
        parse_environment = False
        my_result = None
        my_site = None

        # Update stat record for submit file
        try:
            my_stats = os.stat(submit_file)
        except OSError:
	    # Could not stat file
            logger.error("stat %s" % (submit_file))
            return my_result, my_site

        # Check submit file timestamp
        if stamp < my_stats[8]: #mtime
            logger.info("%s: sub file modified: job timestamp=%d, file mtime=%d, diff=%d" %
                        (submit_file, stamp, my_stats[8], my_stats[8]-stamp))

        # Check if we need to parse the environment line
        if (self._exec_job_id.startswith("pegasus-plan") or
            self._exec_job_id.startswith("subdax_")):
            parse_environment = True

        try:
            SUB = open(submit_file, "r")
        except IOError:
            logger.error("unable to parse %s" % (submit_file))
            return my_result, my_site

        # Parse submit file
        for my_line in SUB:
            if re_rsl_string.search(my_line):
                # Found RSL string, do parse now
                for my_match in re.findall(r"\(([^)]+)\)", my_line):
                    # Split into key and value
                    my_k, my_v = my_match.split("=", 1)
                    # Remove _- characters from string
                    my_k = re_rsl_clean.sub('', my_k)
                    if my_k.lower() in good_rsl and my_v > my_result:
                        try:
                            my_result = int(my_v)
                        except ValueError:
                            my_result = None
            elif re_site_parse_gvds.search(my_line):
                # GVDS agreement
                my_site = re_site_parse_gvds.search(my_line).group(4)
                self._site_name = my_site
            elif re_site_parse_euryale.search(my_line):
                # Euryale specific comment
                my_site = re_site_parse_euryale.search(my_line).group(1)
                self._site_name = my_site
            elif re_parse_transformation.search(my_line):
                # Found line with job transformation
                my_transformation = re_parse_transformation.search(my_line).group(1)
                # Remove quotes, if any
                my_transformation = my_transformation.strip('"')
                self._main_job_transformation = my_transformation
            elif re_parse_derivation.search(my_line):
                # Found line with job derivation
                my_derivation = re_parse_derivation.search(my_line).group(1)
                # Remove quotes, if any
                my_derivation = my_derivation.strip('"')
                if my_derivation == "null":
                    # If derivation is the "null" string, we don't want to take it
                    self._main_job_derivation = None
                else:
                    self._main_job_derivation = my_derivation
            elif re_parse_executable.search(my_line):
                # Found line with executable
                my_executable = re_parse_executable.search(my_line).group(1)
                # Remove quotes, if any
                my_executable = my_executable.strip('"')
                self._main_job_executable = my_executable
            elif re_parse_arguments.search(my_line):
                # Found line with arguments
                my_arguments = re_parse_arguments.search(my_line).group(1)
                # Remove quotes, if any
                my_arguments = my_arguments.strip('"')
                self._main_job_arguments = my_arguments
            elif re_parse_multiplier_factor.search(my_line):
                # Found line with multiplier_factor
                my_multiplier_factor = re_parse_multiplier_factor.search(my_line).group(1)
                try:
                    self._main_job_multiplier_factor = int(my_multiplier_factor)
                except ValueError:
                    logger.warning("%s: cannot convert multiplier factor: %s" % (os.path.basename(submit_file),
                                                                                 my_multiplier_factor))
                    self._main_job_multiplier_factor = None
            elif re_parse_input.search(my_line):
                # Found line with input file
                my_input = re_parse_input.search(my_line).group(1)
                # Remove quotes, if any
                my_input = my_input.strip('"')
                self._input_file = os.path.normpath(my_input)
            elif re_parse_output.search(my_line):
                # Found line with output file
                my_output = re_parse_output.search(my_line).group(1)
                # Remove quotes, if any
                my_output = my_output.strip('"')
                self._output_file = os.path.normpath(my_output)
            elif re_parse_error.search(my_line):
                # Found line with error file
                my_error = re_parse_error.search(my_line).group(1)
                # Remove quotes, if any
                my_error = my_error.strip('"')
                self._error_file = os.path.normpath(my_error)
            elif parse_environment and re_parse_environment.search(my_line):
                # Found line with environment
                v = re_parse_environment.search(my_line).group(1)
                sub_props = v.split(';')
                for sub_prop_line in sub_props:
                    sub_prop_line = sub_prop_line.strip() # Remove any spaces
                    if len(sub_prop_line) == 0:
                        continue
                    sub_prop = re_parse_property.search(sub_prop_line)
                    if sub_prop:
                        if sub_prop.group(1) == "_CONDOR_DAGMAN_LOG":
                            self._job_dagman_out = sub_prop.group(2)

        SUB.close()

        # All done!
        return my_result, my_site

    def extract_job_info(self, run_dir, kickstart_output):
        """
        This function reads the output from the kickstart parser and
        extracts the job information for the Stampede schema. It first
        looks for an invocation record, and then for a clustered
        record.

        Returns None if an error occurs, True if an invocation record
        was found, and False if it wasn't.
        """

        # Check if we have anything
        if len(kickstart_output) == 0:
            return None

        # Kickstart was parsed
        self._kickstart_parsed = True

        # Let's try to find an invocation record...
        my_invocation_found = False
        my_task_number = 0
        self._stdout_text = "" # Initialize stdout
        stdout_text_list = []
        stdout_size=0
        for my_record in kickstart_output:
            if not "invocation" in my_record:
                # Not this one... skip to the next
                continue
            # Ok, we have an invocation record, extract the information we
            # need. Note that this may overwrite information obtained from
            # the submit file (e.g. the site_name).
            
            # Increment task_number
            my_task_number = my_task_number + 1

            if not my_invocation_found:
                # Things we only need to do once
                if "resource" in my_record:
                    self._site_name = my_record["resource"]
                if "user" in my_record:
                    self._remote_user = my_record["user"]
                if "cwd" in my_record:
                    self._remote_working_dir = my_record["cwd"]
                if "hostname" in my_record:
                    self._host_id = my_record["hostname"]
            
                # We are done with this part
                my_invocation_found = True

            #PM-641 optimization Modified string concatenation to a list join 
            if "stdout" in my_record:
                if len(my_record["stdout"])<= MAX_OUTPUT_LENGTH - stdout_size:
                    try:
                        stdout_text_list.append(utils.quote("#@ %d stdout\n" % (my_task_number)))
                        stdout_text_list.append(utils.quote(my_record["stdout"]))
                        stdout_text_list.append(utils.quote("\n"))
                        stdout_size+=len(my_record["stdout"])+20
                    except KeyError:
                        logger.exception( "Unable to parse stdout section from kickstart record for task %s from file %s " %(my_task_number, self.get_rotated_out_filename() ))

            if "stderr" in my_record:
                if len(my_record["stderr"]) <= MAX_OUTPUT_LENGTH - stdout_size :
                    try:
                        stdout_text_list.append(utils.quote("#@ %d stderr\n" % (my_task_number)))
                        stdout_text_list.append(utils.quote(my_record["stderr"]))
                        stdout_text_list.append(utils.quote("\n"))
                        stdout_size+=len(my_record["stderr"])+20
                    except KeyError:
                        logger.exception( "Unable to parse stderr section from kickstart record for task %s from file %s " %(my_task_number, self.get_rotated_out_filename() ))

        if len(stdout_text_list) > 0 :
            self._stdout_text = "".join(stdout_text_list)


            #PM-641 optimization merged encoding above
        # Now, we encode it!
#        if self._stdout_text != "":
#            self._stdout_text = utils.quote(self._stdout_text)


        if not my_invocation_found:
            logger.debug("cannot find invocation record in output")

        # Look for clustered record...
        my_cluster_found = False
        for my_record in kickstart_output:
            if not "clustered" in my_record:
                # Not this one... skip to the next
                continue
            # Ok found it, fill in cluster parameters
            if "duration" in my_record:
                self._cluster_duration = my_record["duration"]
            if "start" in my_record:
                # Convert timestamp to EPOCH
                my_start = utils.epochdate(my_record["start"])
                if my_start is not None:
                    self._cluster_start_time = my_start
            # No need to look further...
            my_cluster_found = True
            break

        if not my_cluster_found:
            logger.debug("cannot find cluster record in output")

        # Finally, read error file only 
        my_err_file = os.path.join(run_dir, self._error_file)

        if my_invocation_found:
            # in my job output there were some invocation records
            # assume then that they are rotated also
            my_err_file = my_err_file + ".%03d" % (self._job_output_counter)

        try:
            ERR = open(my_err_file, 'r')
            self._stderr_text = utils.quote(ERR.read())
        except IOError:
            self._stderr_text = None
            logger.warning("unable to read error file: %s, continuing..." % (my_err_file))
        else:
            ERR.close()

        # Done populating Job class with information from the output file
        return my_invocation_found

    def get_rotated_out_filename(self):
        """
        Returns the name of the rotated .out file for the job on the basis
        of the current counter
        """

        basename = self._output_file
        if self._has_rotated_stdout_err_files:
            basename += ".%03d" % ( self._job_output_counter)

        return basename

    def get_rotated_err_filename(self ):
        """
        Returns the name of the rotated .err file for the job on the basis
        of the current counter
        """

        basename = self._error_file
        if self._has_rotated_stdout_err_files:
            basename += ".%03d" % ( self._job_output_counter)

        return basename

    def read_stdout_stderr_files(self, run_dir):
        """
        This function reads both stdout and stderr files and populates
        these fields in the Job class.
        """
        my_max_encoded_length = MAX_OUTPUT_LENGTH - 2000
        if self._output_file is None:
            # This is the case for SUBDAG jobs
            self._stdout_text = None
        else:
            basename = self._output_file
            if self._has_rotated_stdout_err_files:
                basename += ".%03d" % ( self._job_output_counter)

            my_out_file = os.path.join(run_dir, basename)

            try:
                OUT = open(my_out_file, 'r')
                buffer = OUT.read()
                if len( buffer ) > my_max_encoded_length :
                    buffer = buffer[:my_max_encoded_length]
                self._stdout_text = utils.quote("#@ 1 stdout\n" + buffer)
            except IOError:
                self._stdout_text = None
                logger.warning("unable to read output file: %s, continuing..." % (my_out_file))
            else:
                OUT.close()

        if self._error_file is None:
            # This is the case for SUBDAG jobs
            self._stderr_text = None
        else:
            basename = self._error_file
            if self._has_rotated_stdout_err_files:
                basename += ".%03d" % ( self._job_output_counter)

            my_err_file = os.path.join(run_dir, basename)

            try:
                ERR = open(my_err_file, 'r')
                buffer = ERR.read()
                if len( buffer ) > my_max_encoded_length :
                    buffer = buffer[:my_max_encoded_length]
                self._stderr_text = utils.quote(buffer)
            except IOError:
                self._stderr_text = None
                logger.warning("unable to read error file: %s, continuing..." % (my_err_file))
            else:
                ERR.close()
