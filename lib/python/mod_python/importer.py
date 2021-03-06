 # vim: set sw=4 expandtab :
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
 # The code in this file originally donated by Graham Dumpleton.
 # 
 # $Id$

from mod_python import apache
from mod_python import publisher

import os
import sys
import new
import types
import pdb
import imp
import md5
import time
import string
import StringIO
import traceback
import cgi

try:
    import threading
except:
    import dummy_threading as threading


# Define a transient per request modules cache. This is
# not the same as the true global modules cache used by
# the module importer. Instead, the per request modules
# cache is where references to modules loaded in order
# to satisfy the requirements of a specific request are
# stored for the life of that request. Such a cache is
# required to ensure that two distinct bits of code that
# load the same module do in fact use the same instance
# of the module and that an update of the code for a
# module on disk doesn't cause the latter handler code
# to load its own separate instance. This is in part
# necessary because the module loader does not reload a
# module on top of the old module, but loads the new
# instance into a clean module.

class _module_cache(dict): pass

_request_modules_cache = {}

def _cleanup_request_modules_cache(thread=None):
    thread = thread or threading.currentThread()
    _request_modules_cache.pop(thread, None)

def _setup_request_modules_cache(req=None):
    thread = threading.currentThread()
    if not _request_modules_cache.has_key(thread):
        _request_modules_cache[thread] = _module_cache()
        _request_modules_cache[thread].generation = 0
        _request_modules_cache[thread].ctime = 0
        if req:
            req.register_cleanup(_cleanup_request_modules_cache, thread)

def _get_request_modules_cache():
    thread = threading.currentThread()
    return _request_modules_cache.get(thread, None)

def request_modules_graph(verbose=0):
    output = StringIO.StringIO()
    modules = _get_request_modules_cache()
    print >> output, 'digraph REQUEST {'
    print >> output, 'node [shape=box];'

    for module in modules.values():

        name = module.__name__
        filename = module.__file__

        if verbose:
            cache = module.__mp_info__.cache

            ctime = time.asctime(time.localtime(cache.ctime))
            mtime = time.asctime(time.localtime(cache.mtime))
            atime = time.asctime(time.localtime(cache.atime))
            generation = cache.generation
            direct = cache.direct
            indirect = cache.indirect
            path = module.__mp_path__

            message = '%s [label="%s\\nmtime = %s\\nctime = %s\\natime = %s\\n'
            message += 'generation = %d, direct = %d, indirect = %d\\n'
            message += 'path = %s"];'

            print >> output, message % (name, filename, mtime, ctime, atime, \
                    generation, direct, indirect, path)
        else:
            message = '%s [label="%s"];'

            print >> output, message % (name, filename)

        children = module.__mp_info__.children
        for child_name in children:
            print >> output, '%s -> %s' % (name, child_name)

    print >> output, '}'
    return output.getvalue()

apache.request_modules_graph = request_modules_graph


# Define a transient per request cache into which
# the currently active configuration and handler root
# directory pertaining to a request is stored. This is
# done so that it can be accessed directly from the
# module importer function to obtain configuration
# settings indicating if logging and module reloading is
# enabled and to determine where to look for modules.

_current_cache = {}

def _setup_current_cache(config, options, directory):
    thread = threading.currentThread()
    if directory:
        directory = os.path.normpath(directory)
    cache  = _current_cache.get(thread, (None, None, None))
    if config is None and options is None:
        del _current_cache[thread]
    else:
        _current_cache[thread] = (config, options, directory)
    return cache

def get_current_config():
    thread = threading.currentThread()
    config, options, directory = _current_cache.get(thread,
            (apache.main_server.get_config(), None, None))
    return config

def get_current_options():
    thread = threading.currentThread()
    config, options, directory = _current_cache.get(thread,
            (None, apache.main_server.get_options(), None))
    return options

def get_handler_root():
    thread = threading.currentThread()
    config, options, directory = _current_cache.get(thread,
            (None, None, None))
    return directory

apache.get_current_config = get_current_config
apache.get_current_options = get_current_options
apache.get_handler_root = get_handler_root

# Define an alternate implementation of the module
# importer system and substitute it for the standard one
# in the 'mod_python.apache' module.

apache.ximport_module = apache.import_module

def _parent_context():
    # Determine the enclosing module which has called
    # this function. From that module, return the info
    # stashed in it by the module importer system.

    try:
        raise Exception
    except:
        parent = sys.exc_info()[2].tb_frame.f_back
        while (parent and parent.f_globals.has_key('__file__') and
                parent.f_globals['__file__'] == __file__):
            parent = parent.f_back

    if parent and parent.f_globals.has_key('__mp_info__'):
        parent_info = parent.f_globals['__mp_info__']
        parent_path = parent.f_globals['__mp_path__']
        return (parent_info, parent_path)

    return (None, None)

def _find_module(module_name, path):

    # Search the specified path for a Python code module
    # of the specified name. Note that only Python code
    # files with a '.py' extension will be used. Python
    # packages will be ignored.

    for directory in path:
        if directory is not None:
            if directory == '~':
                root = get_handler_root()
                if root is not None:
                    directory = root
            elif directory[:2] == '~/':
                root = get_handler_root()
                if root is not None:
                    directory = os.path.join(root, directory[2:])
            file = os.path.join(directory, module_name) + '.py'
            if os.path.exists(file):
                return file

def import_module(module_name, autoreload=None, log=None, path=None):

    file = None
    import_path = []

    # Deal with explicit references to a code file.
    # Allow some shortcuts for referring to code file
    # relative to handler root or directory of parent
    # module. Those relative to parent module are not
    # allowed if parent module not imported using this
    # module importing system.

    if os.path.isabs(module_name):
        file = module_name

    elif module_name[:2] == '~/':
        directory = get_handler_root()
        if directory is not None:
            file = os.path.join(directory, module_name[2:])

    elif module_name[:2] == './':
        (parent_info, parent_path) = _parent_context()
        if parent_info is not None:
            directory = os.path.dirname(parent_info.file)
            file = os.path.join(directory, module_name[2:])

    elif module_name[:3] == '../':
        (parent_info, parent_path) = _parent_context()
        if parent_info is not None:
            directory = os.path.dirname(parent_info.file)
            file = os.path.join(directory, module_name)

    if file is None:
        # If not an explicit file reference, it is a
        # module name. Determine the list of directories
        # that need to be searched for a module code
        # file. These directories will be, the directory
        # of the parent if also imported using this
        # importer and any specified search path.

        search_path = []

        if path is not None:
            search_path.extend(path)

        (parent_info, parent_path) = _parent_context()
        if parent_info is not None:
            directory = os.path.dirname(parent_info.file)
            search_path.append(directory)

        if parent_path is not None:
            search_path.extend(parent_path)

        options = get_current_options()
        if options.has_key('mod_python.importer.path'):
            directory = eval(options['mod_python.importer.path'])
            search_path.extend(directory)

        # Attempt to find the code file for the module
        # if we have directories to actually search.

        if search_path:
            file = _find_module(module_name, search_path)

    else:

        # For module imported using explicit path, the
        # path argument becomes the special embedded
        # search path for 'import' statement executed
        # within that module.

        if path is not None:
            import_path = path

    # Was a Python code file able to be identified.

    if file is not None:

        # Use the module importing and caching system
        # to load the code from the specified file.

        return _global_modules_cache.import_module(file, autoreload, \
                log, import_path)

    else:
        # If a module code file could not be found,
        # defer to the standard Python module importer.
        # We should always end up here if the request
        # was for a package.

        return __import__(module_name, {}, {}, ['*'])


apache.import_module = import_module


class _CacheInfo:

    def __init__(self, label, file, mtime):
        self.label = label
        self.file = file
        self.mtime = mtime
        self.module = None
        self.instance = 0
        self.generation = 0
        self.children = {}
        self.path = []
        self.atime = 0
        self.ctime = 0
        self.direct = 0
        self.indirect = 0
        self.reload = 0
        self.lock = threading.Lock()

class _InstanceInfo:

    def __init__(self, label, file, cache):
        self.label = label
        self.file = file
        self.cache = cache
        self.children = {}

class _ModuleCache:

    _prefix = "_mp_"

    def __init__(self):
        self._cache = {}
        self._lock1 = threading.Lock()
        self._lock2 = threading.Lock()
        self._generation = 0
        self._frozen = False
        self._directories = {}

    def _log_notice(self, msg):
        pid = os.getpid()
        name = apache.interpreter
        flags = apache.APLOG_NOERRNO|apache.APLOG_NOTICE
        text = "mod_python (pid=%d, interpreter=%s): %s" % (pid, `name`, msg)
        apache.main_server.log_error(text, flags)

    def _log_warning(self, msg):
        pid = os.getpid()
        name = apache.interpreter
        flags = apache.APLOG_NOERRNO|apache.APLOG_WARNING
        text = "mod_python (pid=%d, interpreter=%s): %s" % (pid, `name`, msg)
        apache.main_server.log_error(text, flags)

    def _log_exception(self):
        pid = os.getpid()
        name = apache.interpreter
        flags = apache.APLOG_NOERRNO|apache.APLOG_ERR
        msg = 'Application error'
        text = "mod_python (pid=%d, interpreter=%s): %s" % (pid, `name`, msg)
        apache.main_server.log_error(text, flags)
        etype, evalue, etb = sys.exc_info()
        for text in traceback.format_exception(etype, evalue, etb):
            apache.main_server.log_error(text[:-1], flags)
        etb = None

    def cached_modules(self):
        self._lock1.acquire()
        try:
            return self._cache.keys()
        finally:
            self._lock1.release()

    def module_info(self, label):
        self._lock1.acquire()
        try:
            return self._cache[label]
        finally:
            self._lock1.release()

    def freeze_modules(self):
        self._frozen = True

    def modules_graph(self, verbose=0):
        self._lock1.acquire()
        try:
            output = StringIO.StringIO()
            modules = self._cache
            print >> output, 'digraph GLOBAL {'
            print >> output, 'node [shape=box];'

            for cache in modules.values():

                name = cache.label
                filename = cache.file

                if verbose:
                    ctime = time.asctime(time.localtime(cache.ctime))
                    mtime = time.asctime(time.localtime(cache.mtime))
                    atime = time.asctime(time.localtime(cache.atime))
                    generation = cache.generation
                    direct = cache.direct
                    indirect = cache.indirect
                    path = cache.path

                    message = '%s [label="%s\\nmtime = %s\\nctime = %s\\n'
                    message += 'atime = %s\\ngeneration = %d, direct = %d,'
                    message += 'indirect = %d\\npath = %s"];'

                    print >> output, message % (name, filename, mtime, ctime,
                            atime, generation, direct, indirect, path)
                else:
                    message = '%s [label="%s"];'

                    print >> output, message % (name, filename)

                children = cache.children
                for child_name in children:
                    print >> output, '%s -> %s' % (name, child_name)

            print >> output, '}'
            return output.getvalue()

        finally:
            self._lock1.release()

    def _check_directory(self, file):

        directory = os.path.dirname(file)

        if not directory in self._directories:
            self._directories[directory] = None
            if directory in sys.path:
                msg = 'Module directory listed in "sys.path". '
                msg = msg + 'This may cause problems. Please check code. '
                msg = msg + 'File being imported is "%s".' % file
                self._log_warning(msg)

    def import_module(self, file, autoreload=None, log=None, path=None):

        # Ensure that file name is normalised so all
        # lookups against the cache equate where they
        # are the same file. This isn't necessarily
        # going to work where symlinks are involved, but
        # not much else that can be done in that case.

        file = os.path.normpath(file)

        # Determine the default values for the module
        # autoreloading and logging arguments direct
        # from the Apache configuration rather than
        # having fixed defaults.

        if autoreload is None or log is None:

            config = get_current_config()

            if autoreload is None:
                autoreload = int(config.get("PythonAutoReload", 1))

            if log is None:
                log = int(config.get("PythonDebug", 0))

        # Warn of any instances where a code file is
        # imported from a directory which also appears
        # in 'sys.path'.

        if log:
            self._check_directory(file)

        # Retrieve the parent context. That is, the
        # details stashed into the parent module by the
        # module importing system itself.

        (parent_info, parent_path) = _parent_context()

        # Check for an attempt by the module to import
        # itself.

        if parent_info:
            assert(file != parent_info.file), "Import cycle in %s." % file

        # Retrieve the per request modules cache entry.

        modules = _get_request_modules_cache()

        # Calculate a unique label corresponding to the
        # name of the file which is the module. This
        # will be used as the '__name__' attribute of a
        # module and as key in various tables.

        label = self._module_label(file)

        # See if the requested module has already been
        # imported previously within the context of this
        # request or at least visited by way of prior
        # dependency checks where it was deemed that it
        # didn't need to be reloaded. If it has we can
        # skip any additional dependency checks and use
        # the module already identified. This ensures
        # the same actual module instance is used. This
        # check is also required so that we don't get
        # into cyclical import loops. Still need to at
        # least record the fact that the module is a
        # child of the parent.

        if modules is not None:
            if modules.has_key(label):
                if parent_info:
                    parent_info.children[label] = time.time()
                return modules[label]

        # Now move on to trying to find the actual
        # module.

        try:
            cache = None

            # First determine if the module has been loaded
            # previously. If not already loaded or if a
            # dependency of the module has been changed on disk
            # or reloaded since parent was loaded, must load the
            # module.

            (cache, load) = self._reload_required(modules,
                    label, file, autoreload)

            # Make sure that the cache entry is locked by the
            # thread so that other threads in a multithreaded
            # system don't try and load the same module at the
            # same time.

            cache.lock.acquire()

            # If this per request modules cache has just
            # been created for the first time, record some
            # details in it about current cache state and
            # run time of the request.

            if modules.ctime == 0:
                modules.generation = self._generation
                modules.ctime = time.time()

            # Import module or obtain it from cache as is
            # appropriate.

            if load:

                # Setup a new empty module to load the code for
                # the module into. Increment the instance count
                # and set the reload flag to force a reload if
                # the import fails.

                cache.instance = cache.instance + 1
                cache.reload = 1

                module = imp.new_module(label)

                # If the module was previously loaded we need to
                # manage the transition to the new instance of
                # the module that is being loaded to replace it.
                # This entails calling the special clone method,
                # if provided within the existing module. Using
                # this method the existing module can then
                # selectively indicate what should be transfered
                # over to the next instance of the module,
                # including thread locks. If this process fails
                # the special purge method is called, if
                # provided, to indicate that the existing module
                # is being forcibly purged out of the system. In
                # that case any existing state will not be
                # transferred.

                if cache.module != None:
                    if hasattr(cache.module, "__mp_clone__"):
                        try:
                            # Migrate any existing state data from
                            # existing module instance to new module
                            # instance.

                            if log:
                                msg = "Cloning module '%s'" % file
                                self._log_notice(msg)

                            cache.module.__mp_clone__(module)

                        except:
                            # Forcibly purging module from system.

                            self._log_exception()

                            if log:
                                msg = "Purging module '%s'" % file
                                self._log_notice(msg)

                            if hasattr(cache.module, "__mp_purge__"):
                                try:
                                    cache.module.__mp_purge__()
                                except:
                                    self._log_exception()

                            cache.module = None

                            # Setup a fresh new module yet again.

                            module = imp.new_module(label)

                    if log:
                        if cache.module == None:
                            msg = "Importing module '%s'" % file
                            self._log_notice(msg)
                        else:
                            msg = "Reimporting module '%s'" % file
                            self._log_notice(msg)
                else:
                    if log:
                        msg = "Importing module '%s'" % file
                        self._log_notice(msg)

                # Must add to the module the path to the modules
                # file. This ensures that module looks like a
                # normal module and this path will also be used
                # in certain contexts when the import statement
                # is used within the module.

                module.__file__ = file

                # Setup a new instance object to store in the
                # module. This will refer back to the actual
                # cache entry and is used to record information
                # which is specific to this incarnation of the
                # module when reloading is occuring.

                instance = _InstanceInfo(label, file, cache)

                module.__mp_info__ = instance

                # Cache any additional module search path which
                # should be used for this instance of the module
                # or package. The path shouldn't be able to be
                # changed during the lifetime of the module to
                # ensure that module imports are always done
                # against the same path.

                if path is None:
                    path = []

                module.__mp_path__ = list(path)

                # Place a reference to the module within the
                # request specific cache of imported modules.
                # This makes module lookup more efficient when
                # the same module is imported more than once
                # within the context of a request. In the case
                # of a cyclical import, avoids a never ending
                # recursive loop.

                if modules is not None:
                    modules[label] = module

                # If this is a child import of some parent
                # module, add this module as a child of the
                # parent.

                atime = time.time()

                if parent_info:
                    parent_info.children[label] = atime

                # Perform actual import of the module.

                try:
                    execfile(file, module.__dict__)

                except:

                    # Importation of the module has failed for
                    # some reason. If this is the very first
                    # import of the module, need to discard the
                    # cache entry entirely else a subsequent
                    # attempt to load the module will wrongly
                    # think it was successfully loaded already.

                    if cache.module is None:
                        del self._cache[label]

                    raise

                # Update the cache and clear the reload flag.

                cache.module = module
                cache.reload = 0

                # Need to also update the list of child modules
                # stored in the cache entry with the actual
                # children found during the import. A copy is
                # made, meaning that any future imports
                # performed within the context of the request
                # handler don't result in the module later being
                # reloaded if they change.

                cache.children = dict(module.__mp_info__.children)

                # Create link to embedded path at end of import.

                cache.path = module.__mp_path__

                # Increment the generation count of the global
                # state of all modules. This is used in the
                # dependency management scheme for reloading to
                # determine if a module dependency has been
                # reloaded since it was loaded.

                self._lock2.acquire()
                self._generation = self._generation + 1
                cache.generation = self._generation
                self._lock2.release()

                # Update access time and reset access counts.

                cache.ctime = atime
                cache.atime = atime
                cache.direct = 1
                cache.indirect = 0

            else:

                # Update the cache.

                module = cache.module

                # Place a reference to the module within the
                # request specific cache of imported modules.
                # This makes module lookup more efficient when
                # the same module is imported more than once
                # within the context of a request. In the case
                # of a cyclical import, avoids a never ending
                # recursive loop.

                if modules is not None:
                    modules[label] = module

                # If this is a child import of some parent
                # module, add this module as a child of the
                # parent.

                atime = time.time()

                if parent_info:
                    parent_info.children[label] = atime

                # Didn't need to reload the module so simply
                # increment access counts and last access time.

                cache.atime = atime
                cache.direct = cache.direct + 1

            return module

        finally:
            # Lock on cache object can now be released.

            if cache is not None:
                cache.lock.release()

    def _reload_required(self, modules, label, file, autoreload):

        # Make sure cache lock is always released.

        try:
            self._lock1.acquire()

            # Check if this is a new module.

            if not self._cache.has_key(label):
                mtime = os.path.getmtime(file)
                cache = _CacheInfo(label, file, mtime)
                self._cache[label] = cache
                return (cache, True)

            # Grab entry from cache.

            cache = self._cache[label]

            # Check if reloads have been disabled.
            # Only avoid a reload though if module
            # hadn't been explicitly marked to be
            # reloaded.

            if not cache.reload:
                if self._frozen or not autoreload:
                    return (cache, False)

            # Determine modification time of file.

            try:
                mtime = os.path.getmtime(file)
            except:

                # Must have been removed just then. We return
                # currently cached module and avoid a reload.
                # Defunct module would need to be purged later.

                msg = 'Module code file has been removed. '
                msg = msg + 'This may cause problems. Using cached module. '
                msg = msg + 'File being imported "%s".' % file
                self._log_warning(msg)

                return (cache, False)

            # Check if modification time has changed or
            # if module has been flagged to be reloaded.

            if cache.reload or mtime != cache.mtime:
                cache.mtime = mtime
                return (cache, True)

            # Check if children have changed in any way
            # that would require a reload.

            if cache.children:

                visited = {}
                ancestors = [label]

                for tag in cache.children:

                    # If the child isn't in the cache any longer
                    # for some reason, force a reload.

                    if not self._cache.has_key(tag):
                        return (cache, True)

                    child = self._cache[tag]

                    # Now check the actual child module.

                    if self._check_module(modules, cache, child,
                            visited, ancestors):
                        return (cache, True)

            # No need to reload the module. Module
            # should be cached in the request object by
            # the caller if required.

            return (cache, False)

        finally:
            self._lock1.release()

    def _check_module(self, modules, parent, current, visited, ancestors):

        # Update current modules access statistics.

        current.indirect = current.indirect + 1
        current.atime = time.time()

        # Check if current module has been marked
        # for reloading.

        if current.reload:
            return True

        # Check if current module has been reloaded
        # since the parent was last loaded.

        if current.generation > parent.generation:
            return True

        # If the current module has been visited
        # already, no need to continue further as it
        # should be up to date.

        if visited.has_key(current.label):
            return False

        # Check if current module has been modified on
        # disk since last loaded.

        try:
            mtime = os.path.getmtime(current.file)

            if mtime != current.mtime:
                return True

        except:
            # Current module must have been removed.
            # Don't cause this to force a reload though
            # as can cause problems. Rely on the parent
            # being modified to cause a reload.

            msg = 'Module code file has been removed. '
            msg = msg + 'This may cause problems. Using cached module. '
            msg = msg + 'File being imported "%s".' % current.file
            self._log_warning(msg)

            if modules is not None:
                modules[current.label] = current.module

            return False

        # Check to see if all the children of the
        # current module need updating or are newer than
        # the current module.

        if current.children:

            ancestors = ancestors + [current.label]

            for label in current.children.keys():

                # Check for a child which refers to one of its
                # ancestors. Hopefully this will never occur. If
                # it does we will force a reload every time to
                # highlight there is a problem. Note this does
                # not get detected first time module is loaded,
                # only here on subsequent checks. If reloading
                # is not enabled, then problem will never be
                # detected and flagged.

                if label in ancestors:
                    msg = 'Module imports an ancestor module. '
                    msg = msg + 'This may cause problems. Please check code. '
                    msg = msg + 'File doing import is "%s".' % current.file
                    self._log_warning(msg)
                    return True

                # If the child isn't in the cache any longer for
                # some reason, force a reload.

                if not self._cache.has_key(label):
                    return True

                child = self._cache[label]

                # Recurse back into this function to check
                # child.

                if self._check_module(modules, current, child,
                        visited, ancestors):
                    return True

        # No need to reload the current module. Now safe
        # to mark the current module as having been
        # visited and cache it into the request object
        # for quick later lookup if a parent needs to be
        # reloaded.

        visited[current.label] = current

        if modules is not None:
            modules[current.label] = current.module

        return False

    def _module_label(self, file):

        # The label is used in the __name__ field of the
        # module and then used in determining child
        # module imports. Thus really needs to be
        # unique. We don't really want to use a module
        # name which is a filesystem path. Hope MD5 hex
        # digest is okay.

        return self._prefix + md5.new(file).hexdigest()


_global_modules_cache = _ModuleCache()

def _get_global_modules_cache():
    return _global_modules_cache

apache.freeze_modules = _global_modules_cache.freeze_modules
apache.modules_graph = _global_modules_cache.modules_graph
apache.module_info = _global_modules_cache.module_info


class _ModuleLoader:

    def __init__(self, file):
        self.__file = file

    def load_module(self, fullname):
        return _global_modules_cache.import_module(self.__file)

class _ModuleImporter:

    def find_module(self, fullname, path=None):

        # Return straight away if requested to import a
        # sub module of a package.

        if '.' in fullname:
            return None

        # Retrieve the parent context. That is, the
        # details stashed into the parent module by the
        # module importing system itself. Only consider
        # using the module importing system for 'import'
        # statement if parent module was imported using
        # the same.

        (parent_info, parent_path) = _parent_context()

        if parent_info is None:
            return None

        # Determine the list of directories that need to
        # be searched for a module code file. These
        # directories will be, the directory of the
        # parent and any specified search path. When
        # enabled, the handler root directory will also
        # be searched.

        search_path = []

        directory = os.path.dirname(parent_info.file)
        search_path.append(directory)

        if parent_path is not None:
            search_path.extend(parent_path)

        options = get_current_options()
        if options.has_key('mod_python.importer.path'):
            directory = eval(options['mod_python.importer.path'])
            search_path.extend(directory)

        # Return if we have no search path to search.

        if not search_path:
            return None

        # Attempt to find the code file for the module.

        file = _find_module(fullname, search_path)

        if file is not None:
            return _ModuleLoader(file)

        return None


sys.meta_path.insert(0, _ModuleImporter())



# Replace mod_python.publisher page cache object with a
# dummy object which uses new module importer.

class _PageCache:
    def __getitem__(self,req):
        return import_module(req.filename)

publisher.xpage_cache = publisher.page_cache
publisher.page_cache = _PageCache()


# Define alternate implementations of the top level
# mod_python entry points and substitute them for the
# standard one in the 'mod_python.apache' callback
# object.

_callback = apache._callback

_callback.xConnectionDispatch = _callback.ConnectionDispatch
_callback.xFilterDispatch = _callback.FilterDispatch
_callback.xHandlerDispatch = _callback.HandlerDispatch
_callback.xIncludeDispatch = _callback.IncludeDispatch
_callback.xImportDispatch = _callback.ImportDispatch
_callback.xReportError = _callback.ReportError

_result_warning = """Handler has returned result or raised SERVER_RETURN
exception with argument having non integer type. Type of value returned
was %s, whereas expected """ + str(types.IntType) + "."

_status_values = {
    "postreadrequesthandler":   [ apache.DECLINED, apache.OK ],
    "transhandler":             [ apache.DECLINED ],
    "maptostoragehandler":      [ apache.DECLINED ],
    "inithandler":              [ apache.DECLINED, apache.OK ],
    "headerparserhandler":      [ apache.DECLINED, apache.OK ],
    "accesshandler":            [ apache.DECLINED, apache.OK ],
    "authenhandler":            [ apache.DECLINED ],
    "authzhandler":             [ apache.DECLINED ],
    "typehandler":              [ apache.DECLINED ],
    "fixuphandler":             [ apache.DECLINED, apache.OK ],
    "loghandler":               [ apache.DECLINED, apache.OK ],
    "handler":                  [ apache.DECLINED, apache.OK ],
}

def _execute_target(config, req, object, arg):

    try:
        # Only permit debugging using pdb if Apache has
        # actually been started in single process mode.

        pdb_debug = int(config.get("PythonEnablePdb", "0"))
        one_process = apache.exists_config_define("ONE_PROCESS")

        if pdb_debug and one_process:

            # Don't use pdb.runcall() as it results in
            # a bogus 'None' response when pdb session
            # is quit. With this code the exception
            # marking that the session has been quit is
            # propogated back up and it is obvious in
            # the error message what actually occurred.

            debugger = pdb.Pdb()
            debugger.reset()
            sys.settrace(debugger.trace_dispatch)

            try:
                result = object(arg)

            finally:
                debugger.quitting = 1
                sys.settrace(None)

        else:
            result = object(arg)

    except apache.SERVER_RETURN, value:
        # For a connection handler, there is no request
        # object so this form of response is invalid.
        # Thus exception is reraised to be handled later.

        if not req:
            raise

        # The SERVER_RETURN exception type when raised
        # otherwise indicates an abort from below with
        # value as (result, status) or (result, None) or
        # result.

        if len(value.args) == 2:
            (result, status) = value.args
            if status:
                req.status = status
        else:
            result = value.args[0]

    # Only check type of return value for connection
    # handlers and request phase handlers. The return
    # value of filters are ultimately ignored.

    if not req or req == arg:
        assert (type(result) == types.IntType), _result_warning % type(result)

    return result

def _process_target(config, req, directory, handler, default, arg, silent):

    if not callable(handler):

        # Determine module name and target object.

        parts = handler.split('::', 1)

        module_name = parts[0]

        if len(parts) == 1:
            object_str = default
        else:
            object_str = parts[1]

        # Update 'sys.path'. This will only be done if we
        # have not encountered the current value of the
        # 'PythonPath' config previously.

        if config.has_key("PythonPath"):

            apache._path_cache_lock.acquire()

            try:

                pathstring = config["PythonPath"]
                if not apache._path_cache.has_key(pathstring):
                    newpath = eval(pathstring)
                    apache._path_cache[pathstring] = None
                    sys.path[:] = newpath

            finally:
                apache._path_cache_lock.release()

        # Import module containing target object. Specify
        # the handler root directory in the search path so
        # that it is still checked even if 'PythonPath' set.

        path = []

        if directory:
            path = [directory]

        module = import_module(module_name, path=path)

        # Obtain reference to actual target object.

        object = apache.resolve_object(module, object_str, arg, silent=silent)

    else:

        # Handler is the target object.

        object = handler

    # Lookup expected status values that allow us to
    # continue when multiple handlers exist.

    expected = _status_values.get(default, None)

    # Default to apache.DECLINED unless in content
    # handler phase.

    if not expected or apache.DECLINED not in expected:
        result = apache.OK
    else:
        result = apache.DECLINED

    if object is not None or not silent:

        result = _execute_target(config, req, object, arg)

        # Stop iteration when target object returns a
        # value other than expected values for the phase.

        if expected and result not in expected:
            return (True, result)

    return (False, result)

def ConnectionDispatch(self, conn):
    """     
    This is the dispatcher for connection handlers.
    """     

    # Determine the default handler name.

    default_handler = "connectionhandler"

    # Be cautious and return server error as default.

    result = apache.HTTP_INTERNAL_SERVER_ERROR

    # Setup transient per request modules cache. Note
    # that this cache will always be thrown away when
    # connection handler returns as there is no way to
    # transfer ownership and responsibility for
    # discarding the cache entry to latter handlers.

    _setup_request_modules_cache()

    try:

        try:
            # Cache the server configuration for the
            # current request so that it will be
            # available from within 'import_module()'.

            config = conn.base_server.get_config()
            options = conn.base_server.get_options()
            cache = _setup_current_cache(config, options, None)

            (aborted, result) = _process_target(config=config, req=None,
                    directory=None, handler=conn.hlist.handler,
                    default=default_handler, arg=conn, silent=0)

        finally:
            # Restore any previous cached configuration.
            # There should not actually be any, but this
            # will cause the configuration cache entry to
            # be discarded.

            _setup_current_cache(*cache)

            # Also discard the modules cache entry.

            _cleanup_request_modules_cache()

    except apache.PROG_TRACEBACK, traceblock:

        # Program runtime error flagged by the application.

        debug = int(config.get("PythonDebug", 0))

        try:
            (exc_type, exc_value, exc_traceback) = traceblock
            result = self.ReportError(exc_type, exc_value, exc_traceback,
                    conn=conn, phase="ConnectionHandler",
                    hname=conn.hlist.handler, debug=debug)

        finally:
            exc_traceback = None

    except:

        # Module loading error or some other runtime error.

        debug = int(config.get("PythonDebug", 0))

        try:    
            exc_type, exc_value, exc_traceback = sys.exc_info()
            result = self.ReportError(exc_type, exc_value, exc_traceback,
                    conn=conn, phase="ConnectionHandler",
                    hname=conn.hlist.handler, debug=debug)
        finally:
            exc_traceback = None

    return result

def FilterDispatch(self, filter):
    """     
    This is the dispatcher for input/output filters.
    """     

    # Determine the default handler name.

    if filter.is_input:
        default_handler = "inputfilter"
    else:
        default_handler = "outputfilter"

    # Setup transient per request modules cache. Note
    # that this will only actually do anything in this
    # case if no Python request phase handlers have been
    # specified. A cleanup handler is registered to
    # later discard the cache entry if it was created.

    _setup_request_modules_cache(filter.req)

    try:

        try:
            directory = filter.dir
            handler = filter.handler

            # If directory for filter is not set,
            # then search back through parents and
            # inherit value from parent if found.

            if directory is None:
                parent = filter.parent
                while parent is not None:
                    if parent.directory is not None:
                        directory = parent.directory
                        break
                    parent = parent.parent

            # If directory for filter still not
            # able to be determined, use the server
            # document root.

            if directory is None:
                directory = filter.req.document_root()

            # Expand relative addressing shortcuts.

            if type(handler) == types.StringType:

                if handler[:2] == './':
                    if directory is not None:
                        handler = os.path.join(directory, handler[2:])

                elif handler[:3] == '../':
                    if directory is not None:
                        handler = os.path.join(directory, handler)

            # Cache the server configuration for the
            # current request so that it will be
            # available from within 'import_module()'.

            config = filter.req.get_config()
            options = filter.req.get_options()
            cache = _setup_current_cache(config, options, directory)

            (aborted, result) = _process_target(config=config,
                    req=filter.req, directory=directory,
                    handler=handler, default=default_handler,
                    arg=filter, silent=0)

            if not filter.closed:
                filter.flush()

        finally:
            # Restore any previous cached configuration.

            _setup_current_cache(*cache)

    except apache.PROG_TRACEBACK, traceblock:

        # Program runtime error flagged by the application.

        debug = int(config.get("PythonDebug", 0))

        filter.disable()

        try:
            (exc_type, exc_value, exc_traceback) = traceblock
            result = self.ReportError(exc_type, exc_value,
                    exc_traceback, req=filter.req, filter=filter,
                    phase="Filter (%s)"%filter.name,
                    hname=filter.handler, debug=debug)

        finally:
            exc_traceback = None

    except:

        # Module loading error or some other runtime error.

        debug = int(config.get("PythonDebug", 0))

        filter.disable()

        try:    
            exc_type, exc_value, exc_traceback = sys.exc_info()
            result = self.ReportError(exc_type, exc_value,
                    exc_traceback, req=filter.req, filter=filter,
                    phase="Filter: " + filter.name,
                    hname=filter.handler, debug=debug)
        finally:
            exc_traceback = None

    return apache.OK

def HandlerDispatch(self, req):
    """     
    This is the dispatcher for handler phases.
    """     

    # Cache name of phase in case something changes it.

    phase = req.phase

    # Determine the default handler name.

    default_handler = phase[len("python"):].lower()

    # Be cautious and return server error as default.

    result = apache.HTTP_INTERNAL_SERVER_ERROR

    # Setup transient per request modules cache. Note
    # that this will only do something if this is the
    # first Python request handler phase to be executed.
    # A cleanup handler is registered to later discard
    # the cache entry if it needed to be created.

    _setup_request_modules_cache(req)

    # Cache configuration for later.

    config = req.get_config()
    options = req.get_options()

    try:
        (aborted, hlist) = False, req.hlist

        # The actual handler root is the directory
        # associated with the handler first in the
        # chain. This may be a handler which was called
        # in an earlier phase if the req.add_handler()
        # method was used. The directory for those that
        # follow the first may have been overridden by
        # directory supplied to the req.add_handler()
        # method.

        root = hlist.directory
        parent = hlist.parent
        while parent is not None:
            root = parent.directory
            parent = parent.parent

	# If directory for handler still not able to be
	# determined, use the server document root.

        if root is None:
            root = req.document_root()

        # Iterate over the handlers defined for the
        # current phase and execute each in turn
        # until the last is reached or prematurely
        # aborted.

        while not aborted and hlist.handler is not None:

            try:
                cache = None

                directory = hlist.directory
                handler = hlist.handler

                # If directory for handler is not set,
                # then search back through parents and
                # inherit value from parent if found.
                # This directory is that where modules
                # are searched for first and may not be
                # the same as the handler root if it
                # was supplied explicitly to the method
                # req.add_handler().

                if directory is None:
                    parent = hlist.parent
                    while parent is not None:
                        if parent.directory is not None:
                            directory = parent.directory
                            break
                        parent = parent.parent

                # Expand relative addressing shortcuts.

                if type(handler) == types.StringType:

                    if handler[:2] == './':
                        if directory is not None:
                            handler = os.path.join(directory, handler[2:])

                    elif handler[:3] == '../':
                        if directory is not None:
                            handler = os.path.join(directory, handler)

                # Cache the server configuration for the
                # current request so that it will be
                # available from within 'import_module()'.

                cache = _setup_current_cache(config, options, root)

                (aborted, result) = _process_target(config=config, req=req,
                        directory=directory, handler=handler,
                        default=default_handler, arg=req, silent=hlist.silent)

            finally:
                # Restore any previous cached configuration.

                _setup_current_cache(*cache)

            hlist.next()

    except apache.PROG_TRACEBACK, traceblock:

        # Program runtime error flagged by the application.

        debug = int(config.get("PythonDebug", 0))

        try:
            (exc_type, exc_value, exc_traceback) = traceblock
            result = self.ReportError(exc_type, exc_value,
                    exc_traceback, req=req, phase=phase,
                    hname=hlist.handler, debug=debug)
        finally:
            exc_traceback = None

    except:

        # Module loading error or some other runtime error.

        debug = int(config.get("PythonDebug", 0))

        try:    
            exc_type, exc_value, exc_traceback = sys.exc_info()
            result = self.ReportError(exc_type, exc_value,
                    exc_traceback, req=req, phase=phase,
                    hname=hlist.handler, debug=debug)
        finally:
            exc_traceback = None

    return result

_callback.ConnectionDispatch = new.instancemethod(
        ConnectionDispatch, _callback, apache.CallBack)
_callback.FilterDispatch = new.instancemethod(
        FilterDispatch, _callback, apache.CallBack)
_callback.HandlerDispatch = new.instancemethod(
        HandlerDispatch, _callback, apache.CallBack)

def IncludeDispatch(self, filter, tag, code):

    # Setup transient per request modules cache. Note
    # that this will only actually do anything in this
    # case if no Python request phase handlers have been
    # specified. A cleanup handler is registered to
    # later discard the cache entry if it was created.

    _setup_request_modules_cache(filter.req)

    try:

        try:
            # Cache the server configuration for the
            # current request so that it will be
            # available from within 'import_module()'.

            config = filter.req.get_config()
            options = filter.req.get_options()
            cache = _setup_current_cache(config, options, None)

            debug = int(config.get("PythonDebug", 0))

            if not hasattr(filter.req,"ssi_globals"):
                filter.req.ssi_globals = {}

            filter.req.ssi_globals["filter"] = filter

            class _InstanceInfo:

                def __init__(self, label, file, cache):
                    self.label = label
                    self.file = file
                    self.cache = cache
                    self.children = {}

            filter.req.ssi_globals["__file__"] = filter.req.filename
            filter.req.ssi_globals["__mp_info__"] = _InstanceInfo(
                    None, filter.req.filename, None)
            filter.req.ssi_globals["__mp_path__"] = []

            code = code.replace('\r\n', '\n').rstrip()

            if tag == 'eval':
                result = eval(code, filter.req.ssi_globals)
                if result is not None:
                    filter.write(str(result))
            elif tag == 'exec':
                exec(code, filter.req.ssi_globals)

            filter.flush()

        finally:

            filter.req.ssi_globals["filter"] = None

            # Restore any previous cached configuration.

            _setup_current_cache(*cache)

    except:
        try:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            result = self.ReportError(exc_type, exc_value, exc_traceback,
                                      filter=filter, phase=filter.name,
                                      hname=filter.req.filename, debug=debug)
        finally:
            exc_traceback = None

        raise

    return apache.OK

_callback.IncludeDispatch = new.instancemethod(
        IncludeDispatch, _callback, apache.CallBack)

def ImportDispatch(self, name):

    config = apache.main_server.get_config()

    debug = int(config.get("PythonDebug", "0"))

    # evaluate pythonpath and set sys.path to
    # resulting value if not already done

    if config.has_key("PythonPath"): 
        apache._path_cache_lock.acquire() 
        try: 
            pathstring = config["PythonPath"] 
            if not apache._path_cache.has_key(pathstring): 
                newpath = eval(pathstring) 
                apache._path_cache[pathstring] = None 
                sys.path[:] = newpath 
        finally: 
            apache._path_cache_lock.release()

    # split module::function
    l = name.split('::', 1)
    module_name = l[0]
    func_name = None
    if len(l) != 1:
        func_name = l[1]

    try:
        # Setup transient per request modules cache.
        # Note that this cache will always be thrown
        # away when the module has been imported.

        _setup_request_modules_cache()

        # Import the module.

        module = import_module(module_name, log=debug)

        # Optionally call function within module.

        if func_name:
            getattr(module, func_name)()

    finally:

        # Discard the modules cache entry.

        _cleanup_request_modules_cache()

_callback.ImportDispatch = new.instancemethod(
        ImportDispatch, _callback, apache.CallBack)

def ReportError(self, etype, evalue, etb, conn=None, req=None, filter=None,
                phase="N/A", hname="N/A", debug=0):

    try:
        try:

            if str(etype) == "exceptions.IOError" \
               and str(evalue)[:5] == "Write":

                # If this is an I/O error while writing to
                # client, it is probably better not to try to
                # write to the cleint even if debug is on.

                # XXX Note that a failure to write back data in
                # a response should be indicated by a special
                # exception type which is caught here and not a
                # generic I/O error as there could be false
                # positives. See MODPYTHON-92.

                debug = 0

            # Determine which log function we are going
            # to use to output any messages.

            if filter and not req:
                req = filter.req

            if req:
                log_error = req.log_error
            elif conn:
                log_error = conn.log_error
            else:
                log_error = apache.main_server.log_error

            # Always log the details of any exception.

            pid = os.getpid()
            iname = apache.interpreter
            flags = apache.APLOG_NOERRNO|apache.APLOG_ERR

            text = "mod_python (pid=%d, interpreter=%s, " % (pid, `iname`)
            text = text + "phase=%s, handler=%s)" % (`phase`, `hname`)
            text = text + ": Application error"

            log_error(text, flags)

            if req:
                location = None
                directory = None

                context = req.hlist

                if context:
                    while context.parent != None:
                        context = context.parent

                    location = context.location
                    directory = context.directory

                hostname = req.server.server_hostname
                root = req.document_root()

                log_error('ServerName: %s' % `hostname`, flags)
                log_error('DocumentRoot: %s' % `root`, flags)
                log_error('URI: %s' % `req.uri`, flags)
                log_error('Location: %s' % `location`, flags)
                log_error('Directory: %s' % `directory`, flags)
                log_error('Filename: %s' % `req.filename`, flags)
                log_error('PathInfo: %s' % `req.path_info`, flags)

            tb = traceback.format_exception(etype, evalue, etb)

            for line in tb:
                log_error(line[:-1], flags)

            if not debug or not req:
                return apache.HTTP_INTERNAL_SERVER_ERROR

            output = StringIO.StringIO()

            req.status = apache.HTTP_INTERNAL_SERVER_ERROR
            req.content_type = 'text/html'

            print >> output
            print >> output, '<pre>'
            print >> output, 'MOD_PYTHON ERROR'
            print >> output
            print >> output, 'ProcessId:      %s' % pid
            print >> output, 'Interpreter:    %s' % `iname`

            if req:
                print >> output
                print >> output, 'ServerName:     %s' % `hostname`
                print >> output, 'DocumentRoot:   %s' % `root`
                print >> output
                print >> output, 'URI:            %s' % `req.uri`
                print >> output, 'Location:       %s' % `location`
                print >> output, 'Directory:      %s' % `directory`
                print >> output, 'Filename:       %s' % `req.filename`
                print >> output, 'PathInfo:       %s' % `req.path_info`

            print >> output
            print >> output, 'Phase:          %s' % `phase`
            print >> output, 'Handler:        %s' % cgi.escape(repr(hname))
            print >> output

            for line in tb:
                print >> output, cgi.escape(line)

            modules = _get_request_modules_cache()

            if modules.ctime != 0:
                accessed = time.asctime(time.localtime(modules.ctime))

                print >> output
                print >> output, 'MODULE CACHE DETAILS'
                print >> output
                print >> output, 'Accessed:       %s' % accessed
                print >> output, 'Generation:     %s' % modules.generation
                print >> output

                labels = {}

                keys = modules.keys()
                for key in keys:
                    module = modules[key]
                    labels[module.__file__] = key

                keys = labels.keys()
                keys.sort()

                for key in keys:
                    label = labels[key]

                    module = modules[label]

                    name = module.__name__
                    filename = module.__file__

                    cache = module.__mp_info__.cache

                    ctime = time.asctime(time.localtime(cache.ctime))
                    mtime = time.asctime(time.localtime(cache.mtime))
                    atime = time.asctime(time.localtime(cache.atime))

                    instance = cache.instance
                    generation = cache.generation
                    direct = cache.direct
                    indirect = cache.indirect
                    path = module.__mp_path__

                    print >> output, '%s {' % name
                    print >> output, '  FileName:     %s' % `filename`
                    print >> output, '  Instance:     %s' % instance,
                    if instance == 1 and (cache.reload or \
                            generation > modules.generation):
                        print >> output, '[IMPORT]'
                    elif cache.reload or generation > modules.generation:
                        print >> output, '[RELOAD]'
                    else:
                        print >> output
                    print >> output, '  Generation:   %s' % generation,
                    if cache.reload:
                        print >> output, '[ERROR]'
                    else:
                        print >> output
                    if cache.mtime:
                        print >> output, '  Modified:     %s' % mtime
                    if cache.ctime:
                        print >> output, '  Imported:     %s' % ctime

                    if path:
                        text = ',\n                '.join(map(repr, path))
                        print >> output, '  ModulePath:   %s' % text

                    friends = []
                    children = []

                    if cache.reload:
                        for child in module.__mp_info__.children:
                            entry = modules[child].__mp_info__.file
                            children.append(entry)
                    else:
                        for child in module.__mp_info__.cache.children:
                            entry = modules[child].__mp_info__.file
                            children.append(entry)
                        for child in module.__mp_info__.children:
                            if child not in module.__mp_info__.cache.children:
                                try:
                                    entry = modules[child].__mp_info__.file
                                    friends.append(entry)
                                except:
                                    try:
                                        entry = apache.module_info(child).file
                                        friends.append(entry)
                                    except:
                                        pass

                    children.sort()
                    friends.sort()

                    if children:
                        text = ',\n                '.join(map(repr, children))
                        print >> output, '  Children:     %s' % text

                    if friends:
                        text = ',\n                '.join(map(repr, friends))
                        print >> output, '  Friends:      %s' % text

                    print >> output, '}'
                    print >> output

            print >> output, '</pre>'

            text = output.getvalue()

            if filter:
                filter.write(text)
                filter.flush()
            else:
                req.write(text)

            return apache.DONE

        except:
            # When all else fails try and dump traceback
            # directory standard error and flush it.

            traceback.print_exc()
            sys.stderr.flush()

    finally:
        etb = None

_callback.ReportError = new.instancemethod(
    ReportError, _callback, apache.CallBack)
