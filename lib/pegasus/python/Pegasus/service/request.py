import os
import pwd
import logging

from flask import request, Response, g
import pam

from Pegasus.service import app

log = logging.getLogger(__name__)

class User(object):
    def __init__(self, uid, gid, username, homedir):
        self.uid = uid
        self.gid = gid
        self.username = username
        self.homedir = homedir

    def get_userdata_dir(self):
        return os.path.join(app.config["STORAGE_DIRECTORY"], self.username)

    def get_master_db(self):
        return os.path.join(self.homedir, ".pegasus", "workflow.db")

    def get_master_db_url(self):
        return "sqlite:///%s" % self.get_master_db()

def get_user_by_uid(uid):
    pw = pwd.getpwuid(uid)
    return User(pw.pw_uid, pw.pw_gid, pw.pw_name, pw.pw_dir)

def get_user_by_username(username):
    pw = pwd.getpwnam(username)
    return User(pw.pw_uid, pw.pw_gid, pw.pw_name, pw.pw_dir)

class BaseAuthentication(object):
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def authenticate(self):
        raise Exception("Not implemented")

    def get_user(self):
        raise Exception("Not implemented")

class NoAuthentication(BaseAuthentication):
    def authenticate(self):
        # Always authenticate the user
        return True

    def get_user(self):
        # Just return info for the user running the service
        return get_user_by_uid(os.getuid())

class PAMAuthentication(BaseAuthentication):
    def authenticate(self):
        try:
            return pam.authenticate(self.username, self.password)
        except Exception, e:
            log.exception(e)
            return False

    def get_user(self):
        try:
            return get_user_by_username(self.username)
        except KeyError:
            raise Exception("Invalid user: %s" % self.username)

def basic_auth_response():
    return Response('Basic Auth Required', 401,
                    {'WWW-Authenticate': 'Basic realm="Pegasus Service"'})

@app.before_request
def before():
    cred = request.authorization
    if not cred:
        return basic_auth_response()

    authclass = app.config["AUTHENTICATION"]
    if authclass not in globals():
        log.error("Unknown authentication method: %s", authclass)
        return basic_auth_response()

    Authentication = globals()[authclass]
    auth = Authentication(cred.username, cred.password)
    if not auth.authenticate():
        log.error("Invalid login: %s", cred.username)
        return basic_auth_response()

    g.user = auth.get_user()
    g.username = g.user.username
    g.master_db_url = g.user.get_master_db_url()

    # If required, set uid and gid of handler process
    if os.getuid() != g.user.uid:
        if os.getuid() != 0:
            log.error("Pegasus service must run as root to enable multi-user access")
            return basic_auth_response()

        os.setgid(g.user.gid)
        os.setuid(g.user.uid)

    # TODO Add login page and session for storing authentication status
    # TODO Add URL Processor for /u/<username> to enable user aliasing

