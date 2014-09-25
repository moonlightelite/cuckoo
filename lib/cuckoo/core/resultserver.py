# Copyright (C) 2010-2014 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import socket
import select
import logging
import datetime
import SocketServer
from threading import Event, Thread

from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.exceptions import CuckooOperationalError
from lib.cuckoo.common.exceptions import CuckooCriticalError
from lib.cuckoo.common.exceptions import CuckooResultError
from lib.cuckoo.common.netlog import NetlogParser, BsonParser
from lib.cuckoo.common.utils import create_folder, Singleton, logtime

log = logging.getLogger(__name__)
loghandle = logging.FileHandler("/home/timlau/cuckoo_libvirt/ResultServer.log")
log.addHandler(loghandle)

BUFSIZE = 16 * 1024
EXTENSIONS = {
    NetlogParser: ".raw",
    BsonParser: ".bson",
}

class Disconnect(Exception):
    pass


class ResultServer(SocketServer.ThreadingTCPServer, object):
    """Result server. Singleton!

    This class handles results coming back from the analysis machines.
    """

    __metaclass__ = Singleton

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        log.info("ResultServer starts")
        self.cfg = Config()
        self.analysistasks = {}
        self.analysishandlers = {}

        self.queue_add = args[0]
        self.queue_delete = args[1]

#        if kwargs[
        args1=args[2:]
        kwargs1={}
        print args1
        print kwargs1

        try:
            server_addr = self.cfg.resultserver.ip, self.cfg.resultserver.port
            SocketServer.ThreadingTCPServer.__init__(self,
                                                     server_addr,
                                                     ResultHandler,
                                                     *args1,
                                                     **kwargs1)
        except Exception as e:
            raise CuckooCriticalError("Unable to bind result server on "
                                      "{0}:{1}: {2}".format(
                                          self.cfg.resultserver.ip,
                                          self.cfg.resultserver.port, str(e)))
        else:
            self.addthread = Thread(target=self.add_monitor)
            self.addthread.setDaemon(True)
            self.addthread.start()
            self.delthread = Thread(target=self.del_monitor)
            self.delthread.setDaemon(True)
            self.delthread.start()
            self.servethread = Thread(target=self.serve_forever)
            self.servethread.setDaemon(True)
            self.servethread.start()
            self.addthread.join()
            self.delthread.join()
            self.servethread.join()

            log.info("ResultServer initiated. " + str(self.queue_add) + str(self.queue_delete))

    def add_monitor(self):
        log.info("ResultServer monitoring add_monitor()")
        while True:
            task,machine = self.queue_add.get(block=True)
            log.info("ResultServer add_monitor() new item")
            self.add_task(task, machine)

    def del_monitor(self):
        log.info("ResultServer monitoring del_monitor()")
        while True:
            task,machine = self.queue_delete.get(block=True)
            t = Thread(target=self.del_task, args=(task,machine))
            t.setDaemon(True)
            t.start()
            #self.del_task(task, machine)

    def add_task(self, task, machine):
        """Register a task/machine with the ResultServer."""
        self.analysistasks[machine.ip] = task, machine
        self.analysishandlers[task.id] = []

    def del_task(self, task, machine):
        """Delete ResultServer state and wait for pending RequestHandlers."""
        x = self.analysistasks.pop(machine.ip, None)
        if not x:
            log.warning("ResultServer did not have {0} in its task "
                        "info.".format(machine.ip))
        handlers = self.analysishandlers.pop(task.id, None)
        for h in handlers:
            h.end_request.set()
            h.done_event.wait()

    def register_handler(self, handler):
        """Register a RequestHandler so that we can later wait for it."""
        task, machine = self.get_ctx_for_ip(handler.client_address[0])
        if not task or not machine:
            return False

        self.analysishandlers[task.id].append(handler)

    def get_ctx_for_ip(self, ip):
        """Return state for this IP's task."""
        x = self.analysistasks.get(ip)
        if not x:
            log.critical("ResultServer unable to map ip to "
                         "context: {0}.".format(ip))
            return None, None

        return x

    def build_storage_path(self, ip):
        """Initialize analysis storage folder."""
        task, machine = self.get_ctx_for_ip(ip)
        if not task or not machine:
            return

        return os.path.join(CUCKOO_ROOT, "storage", "analyses", str(task.id))


class ResultHandler(SocketServer.BaseRequestHandler):
    """Result handler.

    This handler speaks our analysis log network protocol.
    """

    def setup(self):
        self.logfd = None
        self.rawlogfd = None
        self.protocol = None
        self.startbuf = ""
        self.end_request = Event()
        self.done_event = Event()
        self.pid, self.ppid, self.procname = None, None, None
        self.server.register_handler(self)

    def finish(self):
        self.done_event.set()

    def wait_sock_or_end(self):
        while True:
            if self.end_request.isSet():
                return False
            rs, _, _ = select.select([self.request], [], [], 1)
            if rs:
                return True

    def read(self, length):
        buf = ""
        while len(buf) < length:
            if not self.wait_sock_or_end():
                raise Disconnect()
            tmp = self.request.recv(length-len(buf))
            if not tmp:
                raise Disconnect()
            buf += tmp

        if isinstance(self.protocol, (NetlogParser, BsonParser)):
            if self.rawlogfd:
                self.rawlogfd.write(buf)
            else:
                self.startbuf += buf
        return buf

    def read_any(self):
        if not self.wait_sock_or_end():
            raise Disconnect()
        tmp = self.request.recv(BUFSIZE)
        if not tmp:
            raise Disconnect()
        return tmp

    def read_newline(self):
        buf = ""
        while "\n" not in buf:
            buf += self.read(1)
        return buf

    def negotiate_protocol(self):
        # Read until newline.
        buf = self.read_newline()

        if "NETLOG" in buf:
            self.protocol = NetlogParser(self)
        elif "BSON" in buf:
            self.protocol = BsonParser(self)
        elif "FILE" in buf:
            self.protocol = FileUpload(self)
        elif "LOG" in buf:
            self.protocol = LogHandler(self)
        else:
            raise CuckooOperationalError("Netlog failure, unknown "
                                         "protocol requested.")

    def handle(self):
        ip, port = self.client_address
        self.connect_time = datetime.datetime.now()
        log.debug("New connection from: {0}:{1}".format(ip, port))

        self.storagepath = self.server.build_storage_path(ip)
        if not self.storagepath:
            return

        # Create all missing folders for this analysis.
        self.create_folders()

        try:
            # Initialize the protocol handler class for this connection.
            self.negotiate_protocol()

            while self.protocol.read_next_message():
                pass
        except CuckooResultError as e:
            log.warning("ResultServer connection stopping because of "
                        "CuckooResultError: %s.", str(e))
        except (Disconnect, socket.error):
            pass
        except:
            log.exception("FIXME - exception in resultserver connection %s",
                          str(self.client_address))

        self.protocol.close()

        if self.logfd:
            self.logfd.close()
        if self.rawlogfd:
            self.rawlogfd.close()

        log.debug("Connection closed: {0}:{1}".format(ip, port))

    def log_process(self, ctx, timestring, pid, ppid, modulepath, procname):
        if self.pid is not None:
            log.debug("ResultServer got a new process message but already "
                      "has pid %d ppid %s procname %s.",
                      pid, str(ppid), procname)
            raise CuckooResultError("ResultServer connection state "
                                    "inconsistent.")

        log.debug("New process (pid={0}, ppid={1}, name={2}, "
                  "path={3})".format(pid, ppid, procname, modulepath))

        # CSV format files are optional.
        if self.server.cfg.resultserver.store_csvs:
            path = os.path.join(self.storagepath, "logs", "%d.csv" % pid)
            self.logfd = open(path, "wb")

        # Raw Bson or Netlog extension.
        ext = EXTENSIONS.get(type(self.protocol), ".raw")
        path = os.path.join(self.storagepath, "logs", str(pid) + ext)
        self.rawlogfd = open(path, "wb")
        self.rawlogfd.write(self.startbuf)

        self.pid, self.ppid, self.procname = pid, ppid, procname

    def log_thread(self, context, pid):
        log.debug("New thread (tid={0}, pid={1})".format(context[3], pid))

    def log_anomaly(self, subcategory, tid, funcname, msg):
        log.debug("Anomaly (tid=%s, category=%s, funcname=%s): %s",
                  tid, subcategory, funcname, msg)

    def log_call(self, context, apiname, modulename, arguments):
        if not self.rawlogfd:
            raise CuckooOperationalError("Netlog failure, call "
                                         "before process.")

        apiindex, status, returnval, tid, timediff = context

        # log.debug("log_call> tid:{0} apiname:{1}".format(tid, apiname))

        current_time = \
            self.connect_time + datetime.timedelta(0, 0, timediff*1000)
        timestring = logtime(current_time)

        argumentstrings = ["{0}->{1}".format(argname, repr(str(r))[1:-1])
                           for argname, r in arguments]

        if self.logfd:
            print >>self.logfd, ",".join("\"{0}\"".format(i) for i in [
                timestring, self.pid, self.procname, tid, self.ppid,
                modulename, apiname, status, returnval] + argumentstrings)

    def log_error(self, emsg):
        log.warning("ResultServer error condition on connection %s "
                    "(pid %s procname %s): %s", str(self.client_address),
                    str(self.pid), str(self.procname), emsg)

    def create_folders(self):
        folders = "shots", "files", "logs"

        for folder in folders:
            try:
                create_folder(self.storagepath, folder=folder)
            except CuckooOperationalError:
                log.error("Unable to create folder %s" % folder)
                return False


class FileUpload(object):
    RESTRICTED_DIRECTORIES = "reports/",

    def __init__(self, handler):
        self.handler = handler
        self.upload_max_size = \
            self.handler.server.cfg.resultserver.upload_max_size
        self.storagepath = self.handler.storagepath
        self.fd = None

    def read_next_message(self):
        # Read until newline for file path, e.g.,
        # shots/0001.jpg or files/9498687557/libcurl-4.dll.bin

        buf = self.handler.read_newline().strip().replace("\\", "/")
        log.debug("File upload request for {0}".format(buf))

        dir_part, filename = os.path.split(buf)

        if "./" in buf or not dir_part:
            raise CuckooOperationalError("FileUpload failure, banned path.")

        for restricted in self.RESTRICTED_DIRECTORIES:
            if restricted in dir_part:
                raise CuckooOperationalError("FileUpload failure, banned path.")

        try:
            create_folder(self.storagepath, dir_part)
        except CuckooOperationalError:
            log.error("Unable to create folder %s" % dir_part)
            return False

        file_path = os.path.join(self.storagepath, buf.strip())

        self.fd = open(file_path, "wb")
        chunk = self.handler.read_any()
        while chunk:
            self.fd.write(chunk)

            if self.fd.tell() >= self.upload_max_size:
                self.fd.write("... (truncated)")
                break

            try:
                chunk = self.handler.read_any()
            except:
                break

        log.debug("Uploaded file length: {0}".format(self.fd.tell()))

    def close(self):
        if self.fd:
            self.fd.close()


class LogHandler(object):
    def __init__(self, handler):
        self.handler = handler
        self.logpath = os.path.join(handler.storagepath, "analysis.log")
        self.fd = self._open()
        log.debug("LogHandler for live analysis.log initialized.")

    def read_next_message(self):
        if not self.fd:
            return False

        buf = self.handler.read_newline()
        if not buf:
            return False
        self.fd.write(buf)
        self.fd.flush()
        return True

    def close(self):
        if self.fd:
            self.fd.close()

    def _open(self):
        if not os.path.exists(self.logpath):
            return open(self.logpath, "wb")
