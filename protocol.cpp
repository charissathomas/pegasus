#include <string.h>
#include <mpi.h>

#include "tools.h"
#include "protocol.h"
#include "failure.h"

#define MAX_MESSAGE 16384

static char buf[MAX_MESSAGE];

void send_stdio_paths(const std::string &outfile, const std::string &errfile) {
    strcpy(buf, outfile.c_str());
    strcpy(buf+outfile.size()+1, errfile.c_str());
    int size = outfile.size()+errfile.size()+2;
    MPI_Bcast(&size, 1, MPI_INT, 0, MPI_COMM_WORLD); // Send message size first
    MPI_Bcast(buf, outfile.size()+errfile.size()+2, MPI_CHAR, 0, MPI_COMM_WORLD); // Then send message
}

void recv_stdio_paths(std::string &outfile, std::string &errfile) {
    int size;
    MPI_Bcast(&size, 1, MPI_INT, 0, MPI_COMM_WORLD); // Get size first
    MPI_Bcast(buf, size, MPI_CHAR, 0, MPI_COMM_WORLD); // Then get message
    outfile = buf;
    errfile = buf+strlen(buf)+1;
}

void send_registration(const std::string &hostname, unsigned int memory, unsigned int cpus) {
    // Send the hostname
    sprintf(buf, "%s %u %u", hostname.c_str(), memory, cpus);
    int size = strlen(buf) + 1;
    MPI_Send(buf, size, MPI_CHAR, 0, TAG_HOSTNAME, MPI_COMM_WORLD);
}

void recv_registration(int &worker, std::string &hostname, unsigned int &memory, unsigned int &cpus) {
    MPI_Status status;
    MPI_Recv(buf, MAX_MESSAGE, MPI_CHAR, MPI_ANY_SOURCE, TAG_HOSTNAME, MPI_COMM_WORLD, &status);
    worker = status.MPI_SOURCE;
    char name[HOST_NAME_MAX];
    hostname = buf;
    sscanf(buf, "%s %u %u", name, &memory, &cpus);
    hostname = name;
}

void recv_hostrank(int &hostrank) {
    MPI_Status status;
    MPI_Recv(&hostrank, 1, MPI_INT, 0, TAG_HOSTRANK, MPI_COMM_WORLD, &status);
}

void send_hostrank(int worker, int hostrank) {
    MPI_Send(&hostrank, 1, MPI_INT, worker, TAG_HOSTRANK, MPI_COMM_WORLD);
}

void send_request(const std::string &name, const std::string &command, const std::string &pegasus_id, unsigned int memory, int worker) {
    
    
    // Pack message
    unsigned size = 0;
    strcpy(buf+size, name.c_str());
    size += name.size() + 1;
    strcpy(buf+size, command.c_str());
    size += command.size() + 1;
    strcpy(buf+size, pegasus_id.c_str());
    size += pegasus_id.size() + 1;
    sprintf(buf+size, "%u", memory);
    size += strlen(buf+size) + 1;
    
    // Send message
    MPI_Send(buf, size, MPI_CHAR, worker, TAG_COMMAND, MPI_COMM_WORLD);
}

void recv_request(std::string &name, std::string &command, std::string &pegasus_id, unsigned int &memory, int &shutdown) {
    // Recv message
    MPI_Status status;
    MPI_Recv(buf, MAX_MESSAGE, MPI_CHAR, MPI_ANY_SOURCE, MPI_ANY_TAG, MPI_COMM_WORLD, &status);
    
    // If the master wants us to shutdown, we just return here
    shutdown = 0;
    if (status.MPI_TAG == TAG_SHUTDOWN) {
        shutdown = 1;
        return;
    }
    
    // Unpack message
    unsigned size = 0;
    name = buf+size;
    size += name.size() + 1;
    command = buf+size;
    size += command.size() + 1;
    pegasus_id = buf+size;
    size += pegasus_id.size() + 1;
    sscanf(buf+size, "%u", &memory);
}

void send_shutdown(int worker) {
    MPI_Send(NULL, 0, MPI_CHAR, worker, TAG_SHUTDOWN, MPI_COMM_WORLD);
}

void send_response(const std::string &name, int exitcode) {
    sprintf(buf, "%d", exitcode);
    strcpy(buf+strlen(buf)+1, name.c_str());
    MPI_Send(buf, strlen(buf)+name.size()+2, MPI_CHAR, 0, TAG_RESULT, MPI_COMM_WORLD);
}

void recv_response(std::string &name, int &exitcode, int &worker) {
    MPI_Status status;
    MPI_Recv(buf, MAX_MESSAGE, MPI_CHAR, MPI_ANY_SOURCE, TAG_RESULT, MPI_COMM_WORLD, &status);
    
    sscanf(buf,"%d",&exitcode);
    name = buf+strlen(buf)+1;
    worker = status.MPI_SOURCE;
}

void send_total_runtime(double total_runtime) {
    MPI_Reduce(&total_runtime, NULL, 1, MPI_DOUBLE, MPI_SUM, 0, MPI_COMM_WORLD);
}

double collect_total_runtimes() {
    double ignore = 0.0;
    double total_runtime = 0.0;
    MPI_Reduce(&ignore, &total_runtime, 1, MPI_DOUBLE, MPI_SUM, 0, MPI_COMM_WORLD);
    return total_runtime;
}