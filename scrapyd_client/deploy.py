#!/usr/bin/env python

import sys
import os
import glob
import tempfile
import shutil
import time
import netrc
import requests
import json
from argparse import ArgumentParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin
from urllib.request import (build_opener, install_opener,
                            HTTPRedirectHandler as UrllibHTTPRedirectHandler, Request, urlopen)
from subprocess import Popen, PIPE, check_call

from w3lib.http import basic_auth_header
from urllib3.filepost import encode_multipart_formdata
import setuptools  # noqa: F401 not used in code but needed in runtime, don't remove!

from scrapy.utils.project import inside_project
from scrapy.utils.conf import get_config, closest_scrapy_cfg

from scrapyd_client.utils import retry_on_eintr

_SETUP_PY_TEMPLATE = """
# Automatically created by: scrapyd-deploy

from setuptools import setup, find_packages

setup(
    name         = 'project',
    version      = '1.0',
    packages     = find_packages(),
    entry_points = {'scrapy': ['settings = %(settings)s']},
)
""".lstrip()


def parse_args():
    parser = ArgumentParser(description="Deploy Scrapy project to Scrapyd server")
    parser.add_argument('target', nargs='?', default='default', metavar='TARGET')
    parser.add_argument("-p", "--project",
                        help="the project name in the TARGET")
    parser.add_argument("-v", "--version",
                        help="the version to deploy. Defaults to current timestamp")
    parser.add_argument("-l", "--list-targets", action="store_true",
                        help="list available targets")
    parser.add_argument("-a", "--deploy-all-targets", action="store_true",
                        help="deploy all targets")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="debug mode (do not remove build dir)")
    parser.add_argument("-L", "--list-projects", metavar="TARGET",
                        help="list available projects in the TARGET")
    parser.add_argument("--egg", metavar="FILE",
                        help="use the given egg, instead of building it")
    parser.add_argument("--build-egg", metavar="FILE",
                        help="only build the egg, don't deploy it")
    parser.add_argument("--include-dependencies", action="store_true",
                        help="include dependencies from requirements.txt in the egg")
    return parser.parse_args()


def main():
    opts = parse_args()
    exitcode = 0
    if not inside_project():
        _log("Error: no Scrapy project found in this location")
        sys.exit(1)

    install_opener(
        build_opener(HTTPRedirectHandler)
    )

    if opts.list_targets:
        for name, target in _get_targets().items():
            print("%-20s %s" % (name, target['url']))
        return

    if opts.list_projects:
        target = _get_target(opts.list_projects)
        req = Request(_url(target, 'listprojects.json'))
        _add_auth_header(req, target)
        f = urlopen(req)
        projects = json.loads(f.read())['projects']
        print(os.linesep.join(projects))
        return

    tmpdir = None

    if opts.build_egg:  # build egg only
        egg, tmpdir = _build_egg(opts)
        _log("Writing egg to %s" % opts.build_egg)
        shutil.copyfile(egg, opts.build_egg)
    elif opts.deploy_all_targets:
        version = None
        for name, target in _get_targets().items():
            if version is None:
                version = _get_version(target, opts)
            _, tmpdir = _build_egg_and_deploy_target(target, version, opts)
            _remove_tmpdir(tmpdir, opts)
    else:  # buld egg and deploy
        target = _get_target(opts.target)
        version = _get_version(target, opts)
        exitcode, tmpdir = _build_egg_and_deploy_target(target, version, opts)
        _remove_tmpdir(tmpdir, opts)

    sys.exit(exitcode)


def _remove_tmpdir(tmpdir, opts):
    if tmpdir:
        if opts.debug:
            _log("Output dir not removed: %s" % tmpdir)
        else:
            shutil.rmtree(tmpdir)


def _build_egg_and_deploy_target(target, version, opts):
    exitcode = 0
    tmpdir = None

    project = _get_project(target, opts)
    if opts.egg:
        _log("Using egg: %s" % opts.egg)
        egg = opts.egg
    else:
        _log("Packing version %s" % version)
        egg, tmpdir = _build_egg(opts)
    if not _upload_egg(target, egg, project, version):
        exitcode = 1
    return exitcode, tmpdir


def _log(message):
    sys.stderr.write(message + os.linesep)


def _fail(message, code=1):
    _log(message)
    sys.exit(code)


def _get_project(target, opts):
    project = opts.project or target.get('project')
    if not project:
        raise _fail("Error: Missing project")
    return project


def _get_targets():
    cfg = get_config()
    baset = dict(cfg.items('deploy')) if cfg.has_section('deploy') else {}
    targets = {}
    if 'url' in baset:
        targets['default'] = baset
    for x in cfg.sections():
        if x.startswith('deploy:'):
            t = baset.copy()
            t.update(cfg.items(x))
            targets[x[7:]] = t
    return targets


def _get_target(name):
    try:
        return _get_targets()[name]
    except KeyError:
        raise _fail("Unknown target: %s" % name)


def _url(target, action):
    return urljoin(target['url'], action)


def _get_version(target, opts):
    version = opts.version or target.get('version')
    if version == 'HG':
        p = Popen(['hg', 'tip', '--template', '{rev}'], stdout=PIPE, universal_newlines=True)
        d = 'r%s' % p.communicate()[0]
        p = Popen(['hg', 'branch'], stdout=PIPE, universal_newlines=True)
        b = p.communicate()[0].strip('\n')
        return '%s-%s' % (d, b)
    elif version == 'GIT':
        p = Popen(['git', 'describe'], stdout=PIPE, universal_newlines=True)
        d = p.communicate()[0].strip('\n')
        if p.wait() != 0:
            p = Popen(['git', 'rev-list', '--count', 'HEAD'], stdout=PIPE, universal_newlines=True)
            d = 'r%s' % p.communicate()[0].strip('\n')

        p = Popen(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                  stdout=PIPE, universal_newlines=True)
        b = p.communicate()[0].strip('\n')
        return '%s-%s' % (d, b)
    elif version:
        return version
    else:
        return str(int(time.time()))


def _upload_egg(target, eggpath, project, version):
    with open(eggpath, 'rb') as f:
        eggdata = f.read()
    data = {
        'project': project,
        'version': version,
        'egg': ('project.egg', eggdata),
    }
    body, content_type = encode_multipart_formdata(data)
    url = _url(target, 'addversion.json')
    headers = {
        'Content-Type': content_type,
        'Content-Length': str(len(body)),
    }
    skip_tls_verify = target.get("skip-tls-verify", False)
    if skip_tls_verify:
        host_header = target.get("host")
        if not host_header:
            _fail("""Missing host header with skip-tls-verify active! 
                    Please set the host in your scrapy.cfg when setting 
                    skip-tls-verify to true!.""")
        headers["Host"] = host_header
    _add_auth_header(headers, target)
    _log('Deploying to project "%s" in %s' % (project, url))
    res = requests.post(
        url, data=body, headers=headers, verify=not skip_tls_verify
    )
    if res.ok:
        _log("Server response (%s):" % res.status_code)
    else:
        _log("Deploy failed (%s):" % res.status_code)
    _log(json.dumps(res.json(), indent=3))
    return True


def _add_auth_header(headers: dict, target):
    if 'username' in target:
        u, p = target.get('username'), target.get('password', '')
        headers['Authorization'] = basic_auth_header(u, p)
    else:  # try netrc
        try:
            host = urlparse(target['url']).hostname
            a = netrc.netrc().authenticators(host)
            headers['Authorization'] = basic_auth_header(a[0], a[2])
        except (netrc.NetrcParseError, IOError, TypeError):
            pass


def _build_egg(opts):
    closest = closest_scrapy_cfg()
    os.chdir(os.path.dirname(closest))
    if not os.path.exists('setup.py'):
        settings = get_config().get('settings', 'default')
        _create_default_setup_py(settings=settings)
    d = tempfile.mkdtemp(prefix="scrapydeploy-")
    o = open(os.path.join(d, "stdout"), "wb")
    e = open(os.path.join(d, "stderr"), "wb")

    if opts.include_dependencies:
        _log('Including dependencies from requirements.txt')
        if not os.path.isfile('requirements.txt'):
            _fail('Error: Missing requirements.txt')
        command = 'bdist_uberegg'
    else:
        command = 'bdist_egg'

    retry_on_eintr(check_call, [sys.executable, 'setup.py', 'clean', '-a', command, '-d', d], stdout=o, stderr=e)
    o.close()
    e.close()

    egg = glob.glob(os.path.join(d, '*.egg'))[0]
    return egg, d


def _create_default_setup_py(**kwargs):
    with open('setup.py', 'w') as f:
        f.write(_SETUP_PY_TEMPLATE % kwargs)


class HTTPRedirectHandler(UrllibHTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = newurl.replace(' ', '%20')
        if code in (301, 307):
            return Request(newurl,
                           data=req.get_data(),
                           headers=req.headers,
                           origin_req_host=req.get_origin_req_host(),
                           unverifiable=True)
        elif code in (302, 303):
            newheaders = dict((k, v) for k, v in req.headers.items()
                              if k.lower() not in ("content-length", "content-type"))
            return Request(newurl,
                           headers=newheaders,
                           origin_req_host=req.get_origin_req_host(),
                           unverifiable=True)
        else:
            raise HTTPError(req.get_full_url(), code, msg, headers, fp)


if __name__ == "__main__":
    main()
