from flexget import plugins as _plugins_mod
from flexget.event import Event
import os
import sys
import logging
import time
from event import add_event_handler, remove_event_handler

log = logging.getLogger('plugin')

__all__ = ['PluginWarning', 'PluginError',
           'PluginDependencyError', 'register_plugin',
           'register_parser_option', 'register_feed_event',
           'get_plugin_by_name', 'get_plugins_by_group',
           'get_plugin_keywords', 'get_plugins_by_event',
           'get_methods_by_event', 'get_events_by_plugin',
           'internet', 'priority']


class PluginDependencyError(Exception):
    """A plugin has requested another plugin by name, but this plugin does not exists"""

    def __init__(self, value, plugin):
        self.value = value
        self.plugin = plugin

    def __str__(self):
        return '%s plugin: %s' % (repr(self.value), repr(self.plugin))


class RegisterException(Exception):

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class PluginWarning(Warning):

    def __init__(self, value, logger=log, **kwargs):
        self.value = value
        self.log = logger
        self.kwargs = kwargs

    def __str__(self):
        return self.value


class PluginError(Exception):

    def __init__(self, value, logger=log, **kwargs):
        self.value = value
        self.log = logger
        self.kwargs = kwargs

    def __str__(self):
        return self.value


class internet(object):
    """
        @internet decorator for plugin event methods.
        Catches all internet related exceptions and raises PluginError with relevant message.
        Feed handles PluginErrors by aborting the feed.
    """

    def __init__(self, logger=None):
        if logger:
            self.log = logger
        else:
            self.log = logging.getLogger('@internet')

    def __call__(self, func):

        def wrapped_func(*args, **kwargs):
            from httplib import BadStatusLine
            import urllib2
            try:
                return func(*args, **kwargs)
            except urllib2.HTTPError, e:
                raise PluginError('HTTPError %s' % e.code, self.log)
            except urllib2.URLError, e:
                log.debug('decorator caught urlerror')
                raise PluginError('URLError %s' % e.reason, self.log)
            except BadStatusLine:
                log.debug('decorator caught badstatusline')
                raise PluginError('Got BadStatusLine', self.log)
            except IOError, e:
                log.debug('decorator caught ioerror')
                if hasattr(e, 'reason'):
                    raise PluginError('Failed to reach server. Reason: %s' % e.reason, self.log)
                elif hasattr(e, 'code'):
                    raise PluginError('The server couldn\'t fulfill the request. Error code: %s' % e.code, self.log)
        return wrapped_func


def priority(value):
    """Priority decorator for event methods"""

    def decorator(target):
        target.priority = value
        return target
    return decorator


def _strip_trailing_sep(path):
    return path.rstrip("\\/")

DEFAULT_PRIORITY = 128

FEED_EVENTS = ['start', 'input', 'metainfo', 'filter', 'download', 'modify', 'output', 'exit']

# map event names to method names
EVENT_METHODS = {
    'start': 'on_feed_start',
    'input': 'on_feed_input',
    'metainfo': 'on_feed_metainfo',
    'filter': 'on_feed_filter',
    'download': 'on_feed_download',
    'modify': 'on_feed_modify',
    'output': 'on_feed_output',
    'exit': 'on_feed_exit',
    'abort': 'on_feed_abort',
    'accept': 'on_entry_accept',
    'reject': 'on_entry_reject',
    'fail': 'on_entry_fail',
    'process_start': 'on_process_start',
    'process_end': 'on_process_end'}

PREFIXES = FEED_EVENTS + ['module', 'plugin', 'source']

plugins = {}
plugins_loaded = False

_parser = None
_plugin_options = []
_new_event_queue = {}


def register_plugin(plugin_class, name, groups=None, builtin=False, debug=False):
    """Registers a plugin."""
    if groups is None:
        groups = []
    global plugins
    if name is None:
        name = plugin_class.__name__
    if name in plugins:
        log.critical('Error while registering plugin %s. %s' % \
            (name, ('A plugin with the name %s is already registered' % name)))
        return
    plugins[name] = PluginInfo(name, plugin_class, groups, builtin, debug)


def register_parser_option(*args, **kwargs):
    """Adds a parser option to the global parser."""
    global _parser, _plugin_options
    _parser.add_option(*args, **kwargs)
    _plugin_options.append((args, kwargs))


def register_feed_event(plugin_class, name, before=None, after=None):
    """Adds a new feed event to the available events."""
    global _new_event_queue, plugins

    if before and after:
        raise RegisterException('You can only give either before or after for a event.')
    if not before and not after:
        raise RegisterException('You must specify either a before or after event.')
    if name in FEED_EVENTS or name in _new_event_queue:
        raise RegisterException('Event %s already exists.' % name)

    def add_event(event_name, plugin_class, before, after):
        if not before is None and not before in FEED_EVENTS:
            return False
        if not after is None and not after in FEED_EVENTS:
            return False
        # add method name to event -> method lookup table
        EVENT_METHODS[event_name] = 'on_feed_' + event_name
        # queue plugin loading for this type
        PREFIXES.append(name)
        # place event in event list
        if before is None:
            FEED_EVENTS.insert(FEED_EVENTS.index(after) + 1, event_name)
        if after is None:
            FEED_EVENTS.insert(FEED_EVENTS.index(before), event_name)

        # create possibly newly available event handlers
        for loaded_plugin in plugins:
            plugins[loaded_plugin].build_event_handlers()

        return True

    # if can't add yet (dependencies) queue addition
    if not add_event(name, plugin_class.__name__, before, after):
        _new_event_queue[name] = [plugin_class.__name__, before, after]

    for event_name, args in _new_event_queue.items():
        if add_event(event_name, *args):
            del _new_event_queue[event_name]


class PluginInfo(dict):
    """
        Allows accessing key/value pairs of this dictionary subclass via
        attributes.  Also instantiates a plugin and initializes properties.
    """

    def __init__(self, name, item_class, groups=None, builtin=False, debug=False):
        if groups is None:
            groups = []
        dict.__init__(self)

        self.name = name
        self.item_class = item_class

        try:
            instance = item_class()
        except Exception:
            raise

        self.instance = instance
        self.groups = groups
        self.builtin = builtin
        self.debug = debug
        self.event_handlers = {}
        self.build_event_handlers()

    def reset_event_handlers(self):
        """Temporary utility method"""
        self.event_handlers = {}
        self.build_event_handlers()
        # TODO: should unregister events (from flexget.event)
        # this method is not used at the moment anywhere ...
        raise NotImplementedError

    def build_event_handlers(self):
        """(Re)build event_handlers in this plugin"""
        for event, method_name in EVENT_METHODS.iteritems():
            if method_name in self.event_handlers:
                continue
            if hasattr(self.instance, method_name):
                method = getattr(self.instance, method_name)
                if not callable(method):
                    continue
                # check for priority decorator
                if hasattr(method, 'priority'):
                    priority = method.priority
                else:
                    priority = DEFAULT_PRIORITY
                event = add_event_handler('plugin.%s.%s' % (self.name, event), method, priority)
                # provides backwards compatibility
                event.plugin = self
                self.event_handlers[method_name] = event

    def __getattr__(self, attr):
        if attr in self:
            return self[attr]
        return dict.__getattribute__(self, attr)

    def __setattr__(self, attr, value):
        self[attr] = value

    def __str__(self):
        return '<PluginInfo(name=%s)>' % self.name

    __repr__ = __str__


def get_standard_plugins_path():
    """Determine a plugin path suitable for general use."""
    path = os.environ.get('FLEXGET_PLUGIN_PATH',
                          os.path.join(os.path.expanduser('~'), '.flexget', 'plugins')).split(os.pathsep)
    # Get rid of trailing slashes, since Python can't handle them when
    # it tries to import modules.
    path = map(_strip_trailing_sep, path)
    path.append(os.path.abspath(os.path.dirname(_plugins_mod.__file__)))
    # search the arch independent path if we can determine that and
    # the plugin is found nowhere else
    if sys.platform != 'win32':
        try:
            from distutils.sysconfig import get_python_lib
        except ImportError:
            # If distutuils is not available, we just won't add that path
            pass
        else:
            archless_path = os.path.join(get_python_lib(), 'flexget', 'plugins')
            if archless_path not in path:
                path.append(archless_path)
    return path


def load_plugins_from_dirs(dirs):
    _plugins_mod.__path__ = map(_strip_trailing_sep, dirs)
    for d in dirs:
        if not d:
            continue
        log.debug('Looking for plugins in %s', d)
        if os.path.isdir(d):
            load_plugins_from_dir(d)


def load_plugins_from_dir(d):
    # Get the list of valid python suffixes for plugins
    # this includes .py, .pyc, and .pyo (depending on if we are running -O)
    # but it doesn't include compiled modules (.so, .dll, etc)
    global _new_event_queue
    import imp
    valid_suffixes = [suffix for suffix, mod_type, flags in imp.get_suffixes()
                              if flags in (imp.PY_SOURCE, imp.PY_COMPILED)]
    plugin_names = set()
    for f in os.listdir(d):
        path = os.path.join(d, f)
        if os.path.isfile(path):
            f_base, ext = os.path.splitext(f)
            if ext in valid_suffixes:
                if f_base == '__init__':
                    continue # don't load __init__.py again
                elif getattr(_plugins_mod, f_base, None):
                    log.warning('Plugin named %s already loaded' % f_base)
                plugin_names.add(f_base)

    for name in plugin_names:
        try:
            exec "import flexget.plugins.%s" % name in {}
        except Exception, e:
            log.critical('Exception while loading plugin %s' % name)
            log.exception(e)
            raise

    if _new_event_queue:
        for event, args in _new_event_queue.iteritems():
            log.error(('Plugin %s requested new event %s, but it could not be created at requested '
                       'point (before, after). Plugin is not working properly.') % (args[0], event))


def load_plugins(parser):
    """Load plugins from the standard plugin paths."""
    global plugins_loaded, _parser, _plugin_options
    if plugins_loaded:
        if parser is not None:
            for args, kwargs in _plugin_options:
                parser.add_option(*args, **kwargs)
        return 0

    # suppress DeprecationWarning's
    import warnings
    warnings.simplefilter('ignore', DeprecationWarning)

    start_time = time.time()
    _parser = parser
    load_plugins_from_dirs(get_standard_plugins_path())
    _parser = None
    took = time.time() - start_time
    plugins_loaded = True
    return took


def get_plugins_by_event(event):
    """Return list of all plugins that hook :event:"""
    result = []
    if not event in EVENT_METHODS:
        raise Exception('Unknown event %s' % event)
    method_name = EVENT_METHODS[event]
    for info in plugins.itervalues():
        instance = info.instance
        if not hasattr(instance, method_name):
            continue
        if callable(getattr(instance, method_name)):
            result.append(info)
    return result


def get_methods_by_event(event):
    """Return plugin methods that hook :event: in order of priority (highest first)."""
    result = []
    if not event in EVENT_METHODS:
        raise Exception('Unknown event %s' % event)
    method_name = EVENT_METHODS[event]
    for info in plugins.itervalues():
        method = info.event_handlers.get(method_name, None)
        if method:
            result.append(method)
    result.sort(reverse=True)
    return result


def get_events_by_plugin(name):
    """Return all events plugin :name: hooks"""
    plugin = get_plugin_by_name(name)
    events = []
    for event_name, method_name in EVENT_METHODS.iteritems():
        if hasattr(plugin.instance, method_name):
            events.append(event_name)
    return events


def get_plugins_by_group(group):
    """Return all plugins with in specified group."""
    res = []
    for info in plugins.itervalues():
        if group in info.get('groups'):
            res.append(info)
    return res


def get_plugin_keywords():
    """Return all registered keywords in a list"""
    keywords = []
    for name in plugins.iterkeys():
        keywords.append(name)
    return keywords


def get_plugin_by_name(name):
    """Get plugin by name, prefered way since this structure may be changed at some point."""
    if not name in plugins:
        raise PluginDependencyError('Unknown plugin %s' % name, name)
    return plugins[name]


# TODO: separate to plugin
def print_doc(plugin_name):
    """Parameter --doc <plugin_name>"""
    found = False
    plugin = plugins.get(plugin_name, None)
    if plugin:
        found = True
        if not plugin.instance.__doc__:
            print 'Plugin %s does not have documentation' % plugin_name
        else:
            print plugin.instance.__doc__
        return
    if not found:
        print 'Could not find plugin %s' % plugin_name
