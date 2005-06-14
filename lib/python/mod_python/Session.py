 #
 # Copyright 2004 Apache Software Foundation 
 # 
 # Licensed under the Apache License, Version 2.0 (the "License"); you
 # may not use this file except in compliance with the License.  You
 # may obtain a copy of the License at
 #
 #      http://www.apache.org/licenses/LICENSE-2.0
 #
 # Unless required by applicable law or agreed to in writing, software
 # distributed under the License is distributed on an "AS IS" BASIS,
 # WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
 # implied.  See the License for the specific language governing
 # permissions and limitations under the License.
 #
 # Originally developed by Gregory Trubetskoy.
 #
 # $Id$

import apache, Cookie
import _apache

import os, os.path
import time
import anydbm, whichdb
import random
import md5
import cPickle, cStringIO
import tempfile
import traceback

COOKIE_NAME="pysid"
DFT_TIMEOUT=30*60 # 30 min
CLEANUP_CHANCE=1000 # cleanups have 1 in CLEANUP_CHANCE chance

tempdir = tempfile.gettempdir()

def _init_rnd():
    """ initialize random number generators
    this is key in multithreaded env, see
    python docs for random """

    # query max number of threads

    
    if _apache.mpm_query(apache.AP_MPMQ_IS_THREADED):
        gennum = _apache.mpm_query(apache.AP_MPMQ_MAX_SPARE_THREADS)
    else:
        gennum = 10

    # make generators
    # this bit is from Python lib reference
    g = random.Random(time.time())
    result = [g]
    for i in range(gennum - 1):
       laststate = g.getstate()
       g = random.Random()
       g.setstate(laststate)
       g.jumpahead(1000000)
       result.append(g)

    return result

rnd_gens = _init_rnd()
rnd_iter = iter(rnd_gens)

def _get_generator():
    # get rnd_iter.next(), or start over
    # if we reached the end of it
    global rnd_iter
    try:
        return rnd_iter.next()
    except StopIteration:
        # the small potential for two threads doing this
        # seems does not warrant use of a lock
        rnd_iter = iter(rnd_gens)
        return rnd_iter.next()

def _new_sid(req):
    # Make a number based on current time, pid, remote ip
    # and two random ints, then hash with md5. This should
    # be fairly unique and very difficult to guess.

    t = long(time.time()*10000)
    pid = os.getpid()
    g = _get_generator()
    rnd1 = g.randint(0, 999999999)
    rnd2 = g.randint(0, 999999999)
    ip = req.connection.remote_ip

    return md5.new("%d%d%d%d%s" % (t, pid, rnd1, rnd2, ip)).hexdigest()
    
class BaseSession(dict):

    def __init__(self, req, sid=None, secret=None, lock=1,
                 timeout=0):

        self._req, self._sid, self._secret = req, sid, secret
        self._lock = lock
        self._new = 1
        self._created = 0
        self._accessed = 0
        self._timeout = 0
        self._locked = 0
        self._invalid = 0
        
        dict.__init__(self)

        if not self._sid:
            # check to see if cookie exists
            if secret:  
                cookies = Cookie.get_cookies(req, Class=Cookie.SignedCookie,
                                             secret=self._secret)
            else:
                cookies = Cookie.get_cookies(req)

            if cookies.has_key(COOKIE_NAME):
                self._sid = cookies[COOKIE_NAME].value

        self.init_lock()

        if self._sid:
            # attempt to load ourselves
            self.lock()
            if self.load():
                self._new = 0

        if self._new:
            # make a new session
            if self._sid: self.unlock() # unlock old sid
            self._sid = _new_sid(self._req)
            self.lock()                 # lock new sid
            Cookie.add_cookie(self._req, self.make_cookie())
            self._created = time.time()
            if timeout:
                self._timeout = timeout
            else:
                self._timeout = DFT_TIMEOUT

        self._accessed = time.time()

        # need cleanup?
        if random.randint(1, CLEANUP_CHANCE) == 1:
            self.cleanup()

    def make_cookie(self):

        if self._secret:
            c = Cookie.SignedCookie(COOKIE_NAME, self._sid,
                                    secret=self._secret)
        else:
            c = Cookie.Cookie(COOKIE_NAME, self._sid)

        config = self._req.get_options()
        if config.has_key("ApplicationPath"):
            c.path = config["ApplicationPath"]
        else:
            # the path where *Handler directive was specified
            dirpath = self._req.hlist.directory 
            if dirpath:
                docroot = self._req.document_root()
                c.path = dirpath[len(docroot):]
            else:
                c.path = '/'

            # Sometimes there is no path, e.g. when Location
            # is used. When Alias or UserDir are used, then
            # the path wouldn't match the URI. In those cases
            # just default to '/'
            if not c.path or not self._req.uri.startswith(c.path):
                c.path = '/'

        return c

    def invalidate(self):
        c = self.make_cookie()
        c.expires = 0
        Cookie.add_cookie(self._req, c)
        self.delete()
        self._invalid = 1

    def load(self):
        dict = self.do_load()
        if dict == None:
            return 0

        if (time.time() - dict["_accessed"]) > dict["_timeout"]:
            return 0

        self._created  = dict["_created"]
        self._accessed = dict["_accessed"]
        self._timeout  = dict["_timeout"]
        self.update(dict["_data"])
        return 1

    def save(self):
        if not self._invalid:
            dict = {"_data"    : self.copy(), 
                    "_created" : self._created, 
                    "_accessed": self._accessed, 
                    "_timeout" : self._timeout}
            self.do_save(dict)

    def delete(self):
        self.do_delete()
        self.clear()

    def init_lock(self):
        pass
            
    def lock(self):
        if self._lock:
            _apache._global_lock(self._req.server, self._sid)
            self._locked = 1
            self._req.register_cleanup(unlock_session_cleanup, self)

    def unlock(self):
        if self._lock and self._locked:
            _apache._global_unlock(self._req.server, self._sid)
            self._locked = 0
            
    def is_new(self):
        return not not self._new

    def id(self):
        return self._sid

    def created(self):
        return self._created

    def last_accessed(self):
        return self._accessed

    def timeout(self):
        return self._timeout

    def set_timeout(self, secs):
        self._timeout = secs

    def cleanup(self):
        self.do_cleanup()

    def __del__(self):
        self.unlock()

def unlock_session_cleanup(sess):
    sess.unlock()

###########################################################################
## DbmSession

def dbm_cleanup(data):
    dbm, server = data
    _apache._global_lock(server, None, 0)
    db = anydbm.open(dbm, 'c')
    try:
        old = []
        s = db.first()
        while 1:
            key, val = s
            dict = cPickle.loads(val)
            try:
                if (time.time() - dict["_accessed"]) > dict["_timeout"]:
                    old.append(key)
            except KeyError:
                old.append(key)
            try:
                s = db.next()
            except KeyError: break

        for key in old:
            try:
                del db[key]
            except: pass
    finally:
        db.close()
        _apache._global_unlock(server, None, 0)

class DbmSession(BaseSession):

    def __init__(self, req, dbm=None, sid=0, secret=None, dbmtype=anydbm,
                 timeout=0, lock=1):

        if not dbm:
            opts = req.get_options()
            if opts.has_key("SessionDbm"):
                dbm = opts["SessionDbm"]
            else:
                dbm = os.path.join(tempdir, "mp_sess.dbm")

        self._dbmfile = dbm
        self._dbmtype = dbmtype

        BaseSession.__init__(self, req, sid=sid, secret=secret,
                             timeout=timeout, lock=lock)

    def _set_dbm_type(self):
        module = whichdb.whichdb(self._dbmfile)
        if module:
            self._dbmtype = __import__(module)
        
    def _get_dbm(self):
        result = self._dbmtype.open(self._dbmfile, 'c')
        if self._dbmtype is anydbm:
            self._set_dbm_type()
        return result

    def do_cleanup(self):
        data = [self._dbmfile, self._req.server]
        self._req.register_cleanup(dbm_cleanup, data)
        self._req.log_error("DbmSession: registered database cleanup.",
                            apache.APLOG_NOTICE)

    def do_load(self):
        _apache._global_lock(self._req.server, None, 0)
        dbm = self._get_dbm()
        try:
            if dbm.has_key(self._sid):
                return cPickle.loads(dbm[self._sid])
            else:
                return None
        finally:
            dbm.close()
            _apache._global_unlock(self._req.server, None, 0)

    def do_save(self, dict):
        _apache._global_lock(self._req.server, None, 0)
        dbm = self._get_dbm()
        try:
            dbm[self._sid] = cPickle.dumps(dict)
        finally:
            dbm.close()
            _apache._global_unlock(self._req.server, None, 0)

    def do_delete(self):
        _apache._global_lock(self._req.server, None, 0)
        dbm = self._get_dbm()
        try:
            try:
                del dbm[self._sid]
            except KeyError: pass
        finally:
            dbm.close()
            _apache._global_unlock(self._req.server, None, 0)

###########################################################################
## FileSession

# Credits : this was initially contributed by dharana <dharana@dharana.net>
class FileSession(BaseSession):

    def __init__(self, req, sid=0, secret=None, timeout=0, lock=1,
                fast_cleanup=True, verify_cleanup=False, grace_period=240):
        
        opts = req.get_options()
        self._sessdir = opts.get('FileSessionDir',tempdir)
        
        self._fast_cleanup = fast_cleanup
        self._verify_cleanup = verify_cleanup
        self._grace_period = grace_period
        if timeout:
            self._cleanup_timeout = timeout
        else:
            self._cleanup_timeout = DFT_TIMEOUT
        
        BaseSession.__init__(self, req, sid=sid, secret=secret,
            timeout=timeout, lock=lock)

    def do_cleanup(self):
        data = {'req':self._req,
                'sessdir':self._sessdir,
                'fast_cleanup':self._fast_cleanup, 
                'verify_cleanup':self._verify_cleanup, 
                'timeout':self._cleanup_timeout,
                'grace_period':self._grace_period}

        self._req.register_cleanup(filesession_cleanup, data)
        self._req.log_error("FileSession: registered filesession cleanup.",
                            apache.APLOG_NOTICE)

    def do_load(self):
        self.lock_file()
        try:
            try:
                filename = os.path.join(self._sessdir, 'mp_sess_%s' % self._sid)
                fp = file(filename,'rb')
                try:
                    data = cPickle.load(fp)
                    if (time.time() - data["_accessed"]) <= data["_timeout"]:
                        # Change the file access time to the current time so the 
                        # cleanup does not delete this file before the request
                        # can save it's session data
                        os.utime(filename,None)
                    return data
                finally:
                    fp.close()
            except:
                s = cStringIO.StringIO()
                traceback.print_exc(file=s)
                s = s.getvalue()
                self._req.log_error('Error while loading a session : %s'%s)
                return None
        finally:
            self.unlock_file()

    def do_save(self, dict):
        self.lock_file()
        try:
            try:
                filename = os.path.join(self._sessdir, 'mp_sess_%s' % self._sid)
                fp = file(filename, 'wb')
                try:
                    cPickle.dump(dict, fp, 2)
                finally:
                    fp.close()
            except:
                s = cStringIO.StringIO()
                traceback.print_exc(file=s)
                s = s.getvalue()
                self._req.log_error('Error while saving a session : %s'%s)
        finally:
            self.unlock_file()

    def do_delete(self):
        self.lock_file()
        try:
            try:
                os.unlink(os.path.join(self._sessdir, 'mp_sess_%s' % self._sid))
            except Exception:
                pass
        finally:
            self.unlock_file()

    def lock_file(self):
        # self._lock = 1 indicates that session locking is turned on,
        # so let BaseSession handle it.
        # Otherwise, explicitly acquire a lock for the file manipulation.
        if not self._locked:
            _apache._global_lock(self._req.server, self._sid)
            self._locked = 1

    def unlock_file(self):
        if self._locked and not self._lock:
            _apache._global_unlock(self._req.server, self._sid)
            self._locked = 0


def filesession_cleanup(data):
    # There is a small chance that a the cleanup for a given session file
    # may occur at the exact time that the session is being accessed by
    # another request. It is possible under certain circumstances for that
    # session file to be saved in another request only to immediately deleted
    # by the cleanup. To avoid this race condition, a session is allowed a 
    # grace_period before it is considered for deletion by the cleanup. 
    # As long as the grace_period is longer that the time it takes to complete
    # the request (which should normally be less than 1 second), the session will
    # not be mistakenly deleted by the cleanup. By doing this we also avoid the
    # need to lock individual sessions and bypass any potential deadlock
    # situations.
   
    req = data['req']
    sessdir = data['sessdir']
    fast_cleanup = data['fast_cleanup']
    verify_cleanup = data['verify_cleanup']
    timeout = data['timeout']
    grace_period = data['grace_period']

    req.log_error('Sessions cleanup (fast=%s, verify=%s) ...'
        % (fast_cleanup,verify_cleanup),
        apache.APLOG_NOTICE)

    lockfile = os.path.join(sessdir,'.mp_sess.lck')
    try:
        lockfp = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0660) 
    except:
        req.log_error('filesession_cleanup: another process is already running.') 
        return

    try:
        start_time = time.time()
        filelist =  os.listdir(sessdir)
        count = 0
        i = 0
        for f in filelist:
            if not f.startswith('mp_sess_'):
                continue
            try:
                filename = os.path.join(sessdir, f)
                if fast_cleanup:
                    accessed = os.stat(filename).st_mtime
                    if time.time() - accessed < (timeout + grace_period):
                        continue

                if fast_cleanup and not verify_cleanup:        
                    delete_session = True
                else:
                    try:
                        fp = file(filename)
                        dict = cPickle.load(fp)
                        if (time.time() - dict['_accessed']) > (dict['_timeout'] + grace_period):
                            delete_session = True
                        else:
                            delete_session = False
                    finally:
                        fp.close()
                if delete_session:
                    os.unlink(filename)
                    count += 1
            except:
                s = cStringIO.StringIO()
                traceback.print_exc(file=s)
                s = s.getvalue()
                req.log_error('Error while cleaning up the sessions : %s' % s)

        elapsed_time = time.time() - start_time
        req.log_error('filesession cleanup: %d of %d in %.4f seconds' % (count,len(filelist), elapsed_time))
   
        try:
            os.unlink(lockfile)
        except:
            pass

    finally:
        os.close(lockfp)

###########################################################################
## MemorySession

def mem_cleanup(sdict):
    for sid in sdict.keys():
        try:
            session = sdict[sid]
            if (time.time() - session["_accessed"]) > session["_timeout"]:
                del sdict[sid]
        except:
            pass

class MemorySession(BaseSession):

    sdict = {}

    def __init__(self, req, sid=0, secret=None, timeout=0, lock=1):

        BaseSession.__init__(self, req, sid=sid, secret=secret,
                             timeout=timeout, lock=lock)

    def do_cleanup(self):
        self._req.register_cleanup(mem_cleanup, MemorySession.sdict)
        self._req.log_error("MemorySession: registered session cleanup.",
                            apache.APLOG_NOTICE)

    def do_load(self):
        if MemorySession.sdict.has_key(self._sid):
            return MemorySession.sdict[self._sid]
        return None

    def do_save(self, dict):
        MemorySession.sdict[self._sid] = dict

    def do_delete(self):
        try:
            del MemorySession.sdict[self._sid]
        except KeyError: pass

###########################################################################
## Session

def Session(req, sid=0, secret=None, timeout=0, lock=1):

    threaded = _apache.mpm_query(apache.AP_MPMQ_IS_THREADED)
    forked = _apache.mpm_query(apache.AP_MPMQ_IS_FORKED)
    daemons =  _apache.mpm_query(apache.AP_MPMQ_MAX_DAEMONS)

    if (threaded and ((not forked) or (daemons == 1))):
        return MemorySession(req, sid=sid, secret=secret,
                             timeout=timeout, lock=lock)
    else:
        return DbmSession(req, sid=sid, secret=secret,
                          timeout=timeout, lock=lock)

    
###########################################################################
## TestSession

# TestSession is here to test the req_get_session method in requestobject.c
# and will likely disappear in the near future.
   
class TestSession(object):

    def __init__(self, req, sid=0, secret=None, timeout=0, lock=1):
        #req.log_error("TestSession.__init__")
        # Circular Reference causes problem
        self._req = req
        self._lock = 1
        self._locked = 0
        self._sid = _new_sid(req)
        self.lock()
        self.unlock()

    def lock(self):
        if self._lock:
            _apache._global_lock(self._req.server, self._sid)
            self._locked = 1

    def unlock(self):
        if self._lock and self._locked:
            _apache._global_unlock(self._req.server, self._sid)
            self._locked = 0

    def is_new(self):
        return True

    def id(self):
        return self._sid

    def save(self):
        pass

    def load(self):
        pass


# create_session() is called by req.get_session() to create a new
# session instance.
# For now it's just returning a TestSession object but in the future
# it will get the session configuration specified in the apache config
# and return the appropriate session object.

def create_session(req,sid):
    return TestSession(req,sid)
