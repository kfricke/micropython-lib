import sys
import uos as os
import uerrno as errno
import ujson as json
import uzlib
import upip_utarfile as tarfile


DEFAULT_MICROPYPATH = "~/.micropython/lib:/usr/lib/micropython"

debug = False
install_path = None
cleanup_files = []

file_buf = bytearray(512)

class NotFoundError(Exception):
    pass

def op_split(path):
    if path == "":
        return ("", "")
    r = path.rsplit("/", 1)
    if len(r) == 1:
        return ("", path)
    head = r[0]
    if not head:
        head = "/"
    return (head, r[1])

def op_basename(path):
    return op_split(path)[1]

# Expects *file* name
def _makedirs(name, mode=0o777):
    ret = False
    s = ""
    for c in name.rstrip("/").split("/")[:-1]:
        if s:
            s += "/"
        s += c
        try:
            os.mkdir(s)
            ret = True
        except OSError as e:
            if e.args[0] != errno.EEXIST and e.args[0] != errno.EISDIR:
                raise
            ret = False
    return ret


def save_file(fname, subf):
    global file_buf
    with open(fname, "wb") as outf:
        while True:
            sz = subf.readinto(file_buf)
            if not sz:
                break
            outf.write(file_buf, sz)

def install_tar(f, prefix):
    meta = {}
    for info in f:
        #print(info)
        fname = info.name
        try:
            fname = fname[fname.index("/") + 1:]
        except ValueError:
            fname = ""

        save = True
        for p in ("setup.", "PKG-INFO", "README"):
                #print(fname, p)
                if fname.startswith(p) or ".egg-info" in fname:
                    if fname.endswith("/requires.txt"):
                        meta["deps"] = f.extractfile(info).read()
                    save = False
                    if debug:
                        print("Skipping", fname)
                    break

        if save:
            outfname = prefix + fname
            if info.type != tarfile.DIRTYPE:
                if debug:
                    print("Extracting " + outfname)
                _makedirs(outfname)
                subf = f.extractfile(info)
                save_file(outfname, subf)
    return meta

def expandhome(s):
    if "~/" in s:
        h = os.getenv("HOME")
        s = s.replace("~/", h + "/")
    return s

import ussl
import usocket
warn_ussl = True
def url_open(url):
    global warn_ussl
    proto, _, host, urlpath = url.split('/', 3)
    ai = usocket.getaddrinfo(host, 443)
    #print("Address infos:", ai)
    addr = ai[0][4]

    s = usocket.socket(ai[0][0])
    #print("Connect address:", addr)
    s.connect(addr)

    if proto == "https:":
        s = ussl.wrap_socket(s)
        if warn_ussl:
            print("Warning: %s SSL certificate is not validated" % host)
            warn_ussl = False

    # MicroPython rawsocket module supports file interface directly
    s.write("GET /%s HTTP/1.0\r\nHost: %s\r\n\r\n" % (urlpath, host))
    l = s.readline()
    protover, status, msg = l.split(None, 2)
    if status != b"200":
        if status == b"404":
            print("Package not found")
        raise ValueError(status)
    while 1:
        l = s.readline()
        if not l:
            raise ValueError("Unexpected EOF")
        if l == b'\r\n':
            break

    return s


def get_pkg_metadata(name):
    f = url_open("https://pypi.python.org/pypi/%s/json" % name)
    s = f.read()
    f.close()
    return json.loads(s)


def fatal(msg):
    print(msg)
    sys.exit(1)

def install_pkg(pkg_spec, install_path):
    data = get_pkg_metadata(pkg_spec)

    latest_ver = data["info"]["version"]
    packages = data["releases"][latest_ver]
    assert len(packages) == 1
    package_url = packages[0]["url"]
    print("Installing %s %s from %s" % (pkg_spec, latest_ver, package_url))
    package_fname = op_basename(package_url)
    f1 = url_open(package_url)
    f2 = uzlib.DecompIO(f1, 16 + 15)
    f3 = tarfile.TarFile(fileobj=f2)
    meta = install_tar(f3, install_path)
    f1.close()
    return meta

def install(to_install, install_path=None):
    if install_path is None:
        install_path = get_install_path()
    if install_path[-1] != "/":
        install_path += "/"
    if not isinstance(to_install, list):
        to_install = [to_install]
    print("Installing to: " + install_path)
    # sets would be perfect here, but don't depend on them
    installed = []
    try:
        while to_install:
            if debug:
                print("Queue:", to_install)
            pkg_spec = to_install.pop(0)
            if pkg_spec in installed:
                continue
            meta = install_pkg(pkg_spec, install_path)
            installed.append(pkg_spec)
            if debug:
                print(meta)
            deps = meta.get("deps", "").rstrip()
            if deps:
                deps = deps.decode("utf-8").split("\n")
                to_install.extend(deps)
    except NotFoundError:
        print("Error: cannot find '%s' package (or server error), packages may be partially installed" \
            % pkg_spec, file=sys.stderr)

def get_install_path():
    global install_path
    if install_path is None:
        if hasattr(os, "getenv"):
            install_path = os.getenv("MICROPYPATH")
        if install_path is None:
            # sys.path[0] is current module's path
            install_path = sys.path[1]
    install_path = install_path.split(":", 1)[0]
    install_path = expandhome(install_path)
    return install_path

def cleanup():
    for fname in cleanup_files:
        try:
            os.unlink(fname)
        except OSError:
            print("Warning: Cannot delete " + fname)

def help():
    print("""\
upip - Simple PyPI package manager for MicroPython
Usage: micropython -m upip install [-p <path>] <package>... | -r <requirements.txt>

If -p is not given, packages will be installed to first path component of
MICROPYPATH, or to ~/.micropython/lib/ by default.
Note: only MicroPython packages (usually, micropython-*) are supported for
installation, upip does not support arbitrary code in setup.py.""")
    sys.exit(1)

def main():
    global debug
    global install_path
    install_path = None

    if len(sys.argv) < 2 or sys.argv[1] == "-h" or sys.argv[1] == "--help":
        help()

    if sys.argv[1] != "install":
        fatal("Only 'install' command supported")

    to_install = []

    i = 2
    while i < len(sys.argv) and sys.argv[i][0] == "-":
        opt = sys.argv[i]
        i += 1
        if opt == "-h" or opt == "--help":
            help()
        elif opt == "-p":
            install_path = sys.argv[i]
            i += 1
        elif opt == "-r":
            list_file = sys.argv[i]
            i += 1
            with open(list_file) as f:
                while True:
                    l = f.readline()
                    if not l:
                        break
                    to_install.append(l.rstrip())
        elif opt == "--debug":
            debug = True
        else:
            fatal("Unknown/unsupported option: " + opt)

    to_install.extend(sys.argv[i:])
    if not to_install:
        help()

    install(to_install)

    if not debug:
        cleanup()


if __name__ == "__main__":
    main()
