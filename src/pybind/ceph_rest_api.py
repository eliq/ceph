#!/usr/bin/python
# vim: ts=4 sw=4 smarttab expandtab

import os
import collections
import ConfigParser
import json
import logging
import logging.handlers
import rados
import textwrap
import xml.etree.ElementTree
import xml.sax.saxutils

import flask
from ceph_argparse import *

#
# Globals
#

APPNAME = '__main__'
DEFAULT_BASEURL = '/api/v0.1'
DEFAULT_ADDR = '0.0.0.0:5000'
DEFAULT_LOG_LEVEL = 'warning'
DEFAULT_CLIENTNAME = 'client.restapi'
DEFAULT_LOG_FILE = '/var/log/ceph/' + DEFAULT_CLIENTNAME + '.log'

app = flask.Flask(APPNAME)

LOGLEVELS = {
    'critical':logging.CRITICAL,
    'error':logging.ERROR,
    'warning':logging.WARNING,
    'info':logging.INFO,
    'debug':logging.DEBUG,
}

# my globals, in a named tuple for usage clarity

glob = collections.namedtuple('gvars', 'cluster urls sigdict baseurl')
glob.cluster = None
glob.urls = {}
glob.sigdict = {}
glob.baseurl = ''

def load_conf(clustername='ceph', conffile=None):
    import contextlib


    class _TrimIndentFile(object):
        def __init__(self, fp):
            self.fp = fp

        def readline(self):
            line = self.fp.readline()
            return line.lstrip(' \t')


    def _optionxform(s):
        s = s.replace('_', ' ')
        s = '_'.join(s.split())
        return s


    def parse(fp):
        cfg = ConfigParser.RawConfigParser()
        cfg.optionxform = _optionxform
        ifp = _TrimIndentFile(fp)
        cfg.readfp(ifp)
        return cfg


    def load(path):
        f = file(path)
        with contextlib.closing(f):
            return parse(f)

    if conffile:
        # from CEPH_CONF
        return load(conffile)
    else:
        for path in [
            '/etc/ceph/{0}.conf'.format(clustername),
            os.path.expanduser('~/.ceph/{0}.conf'.format(clustername)),
            '{0}.conf'.format(clustername),
        ]:
            if os.path.exists(path):
                return load(path)

    raise EnvironmentError('No conf file found for "{0}"'.format(clustername))

def get_conf(cfg, clientname, key):
    try:
        return cfg.get(clientname, 'restapi_' + key)
    except ConfigParser.NoOptionError:
        return None

METHOD_DICT = {'r':['GET'], 'w':['PUT', 'DELETE']}

# XXX this is done globally, and cluster connection kept open; there
# are facilities to pass around global info to requests and to
# tear down connections between requests if it becomes important

def api_setup():
    """
    Initialize the running instance.  Open the cluster, get the command
    signatures, module, perms, and help; stuff them away in the glob.urls
    dict.
    """
    def get_command_descriptions(target=('mon','')):
        ret, outbuf, outs = json_command(glob.cluster, target,
                                         prefix='get_command_descriptions',
                                         timeout=30)
        if ret:
            err = "Can't get command descriptions: {0}".format(outs)
            app.logger.error(err)
            raise EnvironmentError(ret, err)

        try:
            sigdict = parse_json_funcsigs(outbuf, 'rest')
        except Exception as e:
            err = "Can't parse command descriptions: {}".format(e)
            app.logger.error(err)
            raise EnvironmentError(err)
        return sigdict

    conffile = os.environ.get('CEPH_CONF', '')
    clustername = os.environ.get('CEPH_CLUSTER_NAME', 'ceph')
    clientname = os.environ.get('CEPH_NAME', DEFAULT_CLIENTNAME)
    try:
        err = ''
        cfg = load_conf(clustername, conffile)
    except Exception as e:
        err = "Can't load Ceph conf file: " + str(e)
        app.logger.critical(err)
        app.logger.critical("CEPH_CONF: %s", conffile)
        app.logger.critical("CEPH_CLUSTER_NAME: %s", clustername)
        raise EnvironmentError(err)

    client_logfile = '/var/log/ceph' + clientname + '.log'

    glob.cluster = rados.Rados(name=clientname, conffile=conffile)
    glob.cluster.connect()

    glob.baseurl = get_conf(cfg, clientname, 'base_url') or DEFAULT_BASEURL
    if glob.baseurl.endswith('/'):
        glob.baseurl
    addr = get_conf(cfg, clientname, 'public_addr') or DEFAULT_ADDR
    addrport = addr.rsplit(':', 1)
    addr = addrport[0]
    if len(addrport) > 1:
        port = addrport[1]
    else:
        port = DEFAULT_ADDR.rsplit(':', 1)
    port = int(port)

    loglevel = get_conf(cfg, clientname, 'log_level') or DEFAULT_LOG_LEVEL
    logfile = get_conf(cfg, clientname, 'log_file') or client_logfile
    app.logger.addHandler(logging.handlers.WatchedFileHandler(logfile))
    app.logger.setLevel(LOGLEVELS[loglevel.lower()])
    for h in app.logger.handlers:
        h.setFormatter(logging.Formatter(
            '%(asctime)s %(name)s %(levelname)s: %(message)s'))

    glob.sigdict = get_command_descriptions()
    for k in glob.sigdict.keys():
        glob.sigdict[k]['flavor'] = 'mon'

    # osd.0 is designated the arbiter of valid osd commands
    osd_sigdict = get_command_descriptions(target=('osd', 0))

    # shift osd_sigdict keys up to fit at the end of the mon's glob.sigdict
    maxkey = sorted(glob.sigdict.keys())[-1]
    maxkey = int(maxkey.replace('cmd', ''))
    osdkey = maxkey + 1
    for k, v in osd_sigdict.iteritems():
        if concise_sig(v['sig']).startswith('pg'):
            flavor = 'pgid'
        else:
            flavor = 'tellosd'
        newv = v
        newv['flavor'] = flavor
        globk = 'cmd' + str(osdkey)
        glob.sigdict[globk] = newv
        osdkey += 1

    # glob.sigdict maps "cmdNNN" to a dict containing:
    # 'sig', an array of argdescs
    # 'help', the helptext
    # 'module', the Ceph module this command relates to
    # 'perm', a 'rwx*' string representing required permissions, and also
    #    a hint as to whether this is a GET or POST/PUT operation
    # 'avail', a comma-separated list of strings of consumers that should
    #    display this command (filtered by parse_json_funcsigs() above)
    glob.urls = {}
    for cmdnum, cmddict in glob.sigdict.iteritems():
        cmdsig = cmddict['sig']
        flavor = cmddict.get('flavor', 'mon')
        url, params = generate_url_and_params(cmdsig, flavor=flavor)
        perm = cmddict['perm']
        for k in METHOD_DICT.iterkeys():
            if k in perm:
                methods = METHOD_DICT[k]
        urldict = {'paramsig':params,
                   'help':cmddict['help'],
                   'module':cmddict['module'],
                   'perm':perm,
                   'flavor':flavor,
                   'methods':methods,
                  }

        # glob.urls contains a list of urldicts (usually only one long)
        if url not in glob.urls:
            glob.urls[url] = [urldict]
        else:
            # If more than one, need to make union of methods of all.
            # Method must be checked in handler
            methodset = set(methods)
            for old_urldict in glob.urls[url]:
                methodset |= set(old_urldict['methods'])
            methods = list(methodset)
            glob.urls[url].append(urldict)

        # add, or re-add, rule with all methods and urldicts
        app.add_url_rule(url, url, handler, methods=methods)
        url += '.<fmt>'
        app.add_url_rule(url, url, handler, methods=methods)

    app.logger.debug("urls added: %d", len(glob.urls))

    app.add_url_rule('/<path:catchall_path>', '/<path:catchall_path>',
                     handler, methods=['GET', 'PUT'])
    return addr, port


def generate_url_and_params(sig, flavor="mon"):
    """
    Digest command signature from cluster; generate an absolute
    (including glob.baseurl) endpoint from all the prefix words,
    and a list of non-prefix param descs
    """

    url = ''
    params = []
    # the OSD command descriptors don't include the 'tell <target>'
    if flavor == 'tellosd':
        sig = parse_funcsig(
                            ['tell', {'name':'target', 'type':'CephOsdName'}]
                           ) + sig

    for desc in sig:
        # prefixes go in the URL path
        if desc.t == CephPrefix:
            url += '/' + desc.instance.prefix
        # CephChoices with 1 required string (not --) do too, unless
        # we've already started collecting params, in which case they
        # too are params
        elif desc.t == CephChoices and \
             len(desc.instance.strings) == 1 and \
             desc.req and \
             not str(desc.instance).startswith('--') and \
             not params:
                url += '/' + str(desc.instance)
        else:
            # tell/<target> is a weird case; the URL includes what
            # would everywhere else be a parameter
            if flavor == 'tellosd' and  \
              (desc.t, desc.name) == (CephOsdName, 'target'):
                url += '/<target>'
            else:
                params.append(desc)

    return glob.baseurl + url, params

def concise_sig_for_uri(sig, flavor):
    """
    Return a generic description of how one would send a REST request for sig
    """
    prefix = []
    args = []
    ret = ''
    if flavor == 'tellosd':
        ret = 'tell/<osdid>/'
    # 'pgid' flavor doesn't need special handling; params are
    # embedded mid-signature (pgid and cmd are both params)
    for d in sig:
        if d.t == CephPrefix:
            prefix.append(d.instance.prefix)
        else:
            args.append(d.name + '=' + str(d))
    ret += '/'.join(prefix)
    if args:
        ret += '?' + '&'.join(args)
    return ret

def show_human_help(prefix):
    """
    Dump table showing commands matching prefix
    """
    # XXX There ought to be a better discovery mechanism than an HTML table
    s = '<html><body><table border=1><th>Possible commands:</th><th>Method</th><th>Description</th>'

    permmap = {'r':'GET', 'rw':'PUT'}
    line = ''
    for cmdsig in sorted(glob.sigdict.itervalues(), cmp=descsort):
        concise = concise_sig(cmdsig['sig'])
        if cmdsig['flavor'] == 'pgid':
            concise = 'pg/<pgid>/' + concise
        if cmdsig['flavor'] == 'tellosd':
            concise = 'tell/<osdid>/' + concise
        if concise.startswith(prefix):
            line = ['<tr><td>']
            wrapped_sig = textwrap.wrap(
                concise_sig_for_uri(cmdsig['sig'], cmdsig['flavor']), 40
            )
            for sigline in wrapped_sig:
                line.append(flask.escape(sigline) + '\n')
            line.append('</td><td>')
            line.append(permmap[cmdsig['perm']])
            line.append('</td><td>')
            line.append(flask.escape(cmdsig['help']))
            line.append('</td></tr>\n')
            s += ''.join(line)

    s += '</table></body></html>'
    if line:
        return s
    else:
        return ''

@app.before_request
def log_request():
    """
    For every request, log it.  XXX Probably overkill for production
    """
    app.logger.info(flask.request.url + " from " + flask.request.remote_addr + " " + flask.request.user_agent.string)
    app.logger.debug("Accept: %s", flask.request.accept_mimetypes.values())


@app.route('/')
def root_redir():
    return flask.redirect(glob.baseurl)

def make_response(fmt, output, statusmsg, errorcode):
    """
    If formatted output, cobble up a response object that contains the
    output and status wrapped in enclosing objects; if nonformatted, just
    use output.  Return HTTP status errorcode in any event.
    """
    response = output
    if fmt:
        if 'json' in fmt:
            try:
                native_output = json.loads(output or '[]')
                response = json.dumps({"output":native_output,
                                       "status":statusmsg})
            except:
                return flask.make_response("Error decoding JSON from " +
                                           output, 500)
        elif 'xml' in fmt:
            # one is tempted to do this with xml.etree, but figuring out how
            # to 'un-XML' the XML-dumped output so it can be reassembled into
            # a piece of the tree here is beyond me right now.
            #ET = xml.etree.ElementTree
            #resp_elem = ET.Element('response')
            #o = ET.SubElement(resp_elem, 'output')
            #o.text = output
            #s = ET.SubElement(resp_elem, 'status')
            #s.text = statusmsg
            #response = ET.tostring(resp_elem)
            response = '''
<response>
  <output>
    {0}
  </output>
  <status>
    {1}
  </status>
</response>'''.format(response, xml.sax.saxutils.escape(statusmsg))

    return flask.make_response(response, errorcode)

def handler(catchall_path=None, fmt=None, target=None):
    """
    Main endpoint handler; generic for every endpoint
    """

    ep = catchall_path or flask.request.endpoint
    ep = ep.replace('.<fmt>', '')

    if ep[0] != '/':
        ep = '/' + ep

    # demand that endpoint begin with glob.baseurl
    if not ep.startswith(glob.baseurl):
        return make_response(fmt, '', 'Page not found', 404)

    rel_ep = ep[len(glob.baseurl)+1:]

    # Extensions override Accept: headers override defaults
    if not fmt:
        if 'application/json' in flask.request.accept_mimetypes.values():
            fmt = 'json'
        elif 'application/xml' in flask.request.accept_mimetypes.values():
            fmt = 'xml'

    valid = True
    prefix = ''
    pgid = None

    # Calculate (and possibly validate) target
    if target:
        sig = parse_funcsig([{'name':'target','type':'CephOsdName'}])
        try:
            name = CephOsdName()
            name.valid(target)
            target = (name.nametype, name.nameid)
            prefix = ' '.join(rel_ep.split('/')[2:]).strip()
        except Exception as e:
            valid = False
            reason = str(e)

    elif rel_ep == ('pg') and 'pgid' in flask.request.args:
        pgid = flask.request.args['pgid']
        target = ('pg', pgid)

    if not prefix:
        prefix = ' '.join(rel_ep.split('/')).strip()

    # show "match as much as you gave me" help for unknown endpoints
    if not ep in glob.urls:
        helptext = show_human_help(prefix)
        if helptext:
            resp = flask.make_response(helptext, 400)
            resp.headers['Content-Type'] = 'text/html'
            return resp
        else:
            return make_response(fmt, '', 'Invalid endpoint ' + ep, 400)

    found = None
    exc = ''
    for urldict in glob.urls[ep]:
        if flask.request.method not in urldict['methods']:
            continue
        paramsig = urldict['paramsig']

        # allow '?help' for any specifically-known endpoint
        if 'help' in flask.request.args:
            response = flask.make_response('{0}: {1}'.\
                format(prefix + concise_sig(paramsig), urldict['help']))
            response.headers['Content-Type'] = 'text/plain'
            return response

        # if there are parameters for this endpoint, process them
        if paramsig:
            args = {}
            for k, l in flask.request.args.iterlists():
                if len(l) == 1:
                    args[k] = l[0]
                else:
                    args[k] = l

            # is this a valid set of params?
            try:
                argdict = validate(args, paramsig)
                found = urldict
                break
            except Exception as e:
                exc += str(e)
                continue
        else:
            if flask.request.args:
                continue
            found = urldict
            argdict = {}
            break

    if not found:
        return make_response(fmt, '', exc + '\n', 400)

    argdict['format'] = fmt or 'plain'
    argdict['module'] = found['module']
    argdict['perm'] = found['perm']
    if pgid:
        argdict['pgid'] = pgid

    if not target:
        target = ('mon', '')

    app.logger.debug('sending command prefix %s argdict %s', prefix, argdict)
    ret, outbuf, outs = json_command(glob.cluster, prefix=prefix,
                                     target=target,
                                     inbuf=flask.request.data, argdict=argdict)
    if ret:
        return make_response(fmt, '', 'Error: {0} ({1})'.format(outs, ret), 400)

    response = make_response(fmt, outbuf, outs or 'OK', 200)
    if fmt:
        contenttype = 'application/' + fmt.replace('-pretty','')
    else:
        contenttype = 'text/plain'
    response.headers['Content-Type'] = contenttype
    return response

addr, port = api_setup()
