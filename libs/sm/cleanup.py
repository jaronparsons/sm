#
# Copyright (C) Citrix Systems Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
import os
import os.path
import sys
import time
import signal
import subprocess
import datetime
import traceback
import base64
import zlib
import errno
import stat

from sm.ipc import IPCFlag
from functools import reduce
from time import monotonic as _time
from sm import blktap2
import XenAPI # pylint: disable=import-error

from sm import lvutil
from sm import vhdutil
from sm import lvhdutil
from sm import lvmcache
from sm import journaler
from sm import fjournaler
from sm.core import lock
from sm.core import util
from sm.refcounter import RefCounter
from sm.lvmanager import LVActivator
from sm.srmetadata import LVMMetadataHandler

# Disable automatic leaf-coalescing. Online leaf-coalesce is currently not
# possible due to lvhd_stop_using_() not working correctly. However, we leave
# this option available through the explicit LEAFCLSC_FORCE flag in the VDI
# record for use by the offline tool (which makes the operation safe by pausing
# the VM first)
AUTO_ONLINE_LEAF_COALESCE_ENABLED = True

FLAG_TYPE_ABORT = "abort"     # flag to request aborting of GC/coalesce

# process "lock", used simply as an indicator that a process already exists
# that is doing GC/coalesce on this SR (such a process holds the lock, and we
# check for the fact by trying the lock).
lockGCRunning = None

# process "lock" to indicate that the GC process has been activated but may not
# yet be running, stops a second process from being started.
LOCK_TYPE_GC_ACTIVE = "gc_active"
lockGCActive = None

# Default coalesce error rate limit, in messages per minute. A zero value
# disables throttling, and a negative value disables error reporting.
DEFAULT_COALESCE_ERR_RATE = 1.0 / 60

COALESCE_LAST_ERR_TAG = 'last-coalesce-error'
COALESCE_ERR_RATE_TAG = 'coalesce-error-rate'
VAR_RUN = "/run/"
SPEED_LOG_ROOT = VAR_RUN + "{uuid}.speed_log"

N_RUNNING_AVERAGE = 10

NON_PERSISTENT_DIR = '/run/nonpersistent/sm'

# Signal Handler
SIGTERM = False


class AbortException(util.SMException):
    pass


def receiveSignal(signalNumber, frame):
    global SIGTERM

    util.SMlog("GC: recieved SIGTERM")
    SIGTERM = True
    return


################################################################################
#
#  Util
#
class Util:
    RET_RC = 1
    RET_STDOUT = 2
    RET_STDERR = 4

    UUID_LEN = 36

    PREFIX = {"G": 1024 * 1024 * 1024, "M": 1024 * 1024, "K": 1024}

    def log(text):
        util.SMlog(text, ident="SMGC")
    log = staticmethod(log)

    def logException(tag):
        info = sys.exc_info()
        if info[0] == SystemExit:
            # this should not be happening when catching "Exception", but it is
            sys.exit(0)
        tb = reduce(lambda a, b: "%s%s" % (a, b), traceback.format_tb(info[2]))
        Util.log("*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*")
        Util.log("         ***********************")
        Util.log("         *  E X C E P T I O N  *")
        Util.log("         ***********************")
        Util.log("%s: EXCEPTION %s, %s" % (tag, info[0], info[1]))
        Util.log(tb)
        Util.log("*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*")
    logException = staticmethod(logException)

    def doexec(args, expectedRC, inputtext=None, ret=None, log=True):
        "Execute a subprocess, then return its return code, stdout, stderr"
        proc = subprocess.Popen(args,
                                stdin=subprocess.PIPE, \
                                stdout=subprocess.PIPE, \
                                stderr=subprocess.PIPE, \
                                shell=True, \
                                close_fds=True)
        (stdout, stderr) = proc.communicate(inputtext)
        stdout = str(stdout)
        stderr = str(stderr)
        rc = proc.returncode
        if log:
            Util.log("`%s`: %s" % (args, rc))
        if type(expectedRC) != type([]):
            expectedRC = [expectedRC]
        if not rc in expectedRC:
            reason = stderr.strip()
            if stdout.strip():
                reason = "%s (stdout: %s)" % (reason, stdout.strip())
            Util.log("Failed: %s" % reason)
            raise util.CommandException(rc, args, reason)

        if ret == Util.RET_RC:
            return rc
        if ret == Util.RET_STDERR:
            return stderr
        return stdout
    doexec = staticmethod(doexec)

    def runAbortable(func, ret, ns, abortTest, pollInterval, timeOut):
        """execute func in a separate thread and kill it if abortTest signals
        so"""
        abortSignaled = abortTest()  # check now before we clear resultFlag
        resultFlag = IPCFlag(ns)
        resultFlag.clearAll()
        pid = os.fork()
        if pid:
            startTime = _time()
            try:
                while True:
                    if resultFlag.test("success"):
                        Util.log("  Child process completed successfully")
                        resultFlag.clear("success")
                        return
                    if resultFlag.test("failure"):
                        resultFlag.clear("failure")
                        raise util.SMException("Child process exited with error")
                    if abortTest() or abortSignaled or SIGTERM:
                        os.killpg(pid, signal.SIGKILL)
                        raise AbortException("Aborting due to signal")
                    if timeOut and _time() - startTime > timeOut:
                        os.killpg(pid, signal.SIGKILL)
                        resultFlag.clearAll()
                        raise util.SMException("Timed out")
                    time.sleep(pollInterval)
            finally:
                wait_pid = 0
                rc = -1
                count = 0
                while wait_pid == 0 and count < 10:
                    wait_pid, rc = os.waitpid(pid, os.WNOHANG)
                    if wait_pid == 0:
                        time.sleep(2)
                        count += 1

                if wait_pid == 0:
                    Util.log("runAbortable: wait for process completion timed out")
        else:
            os.setpgrp()
            try:
                if func() == ret:
                    resultFlag.set("success")
                else:
                    resultFlag.set("failure")
            except Exception as e:
                Util.log("Child process failed with : (%s)" % e)
                resultFlag.set("failure")
                Util.logException("This exception has occured")
            os._exit(0)
    runAbortable = staticmethod(runAbortable)

    def num2str(number):
        for prefix in ("G", "M", "K"):
            if number >= Util.PREFIX[prefix]:
                return "%.3f%s" % (float(number) / Util.PREFIX[prefix], prefix)
        return "%s" % number
    num2str = staticmethod(num2str)

    def numBits(val):
        count = 0
        while val:
            count += val & 1
            val = val >> 1
        return count
    numBits = staticmethod(numBits)

    def countBits(bitmap1, bitmap2):
        """return bit count in the bitmap produced by ORing the two bitmaps"""
        len1 = len(bitmap1)
        len2 = len(bitmap2)
        lenLong = len1
        lenShort = len2
        bitmapLong = bitmap1
        if len2 > len1:
            lenLong = len2
            lenShort = len1
            bitmapLong = bitmap2

        count = 0
        for i in range(lenShort):
            val = bitmap1[i] | bitmap2[i]
            count += Util.numBits(val)

        for i in range(i + 1, lenLong):
            val = bitmapLong[i]
            count += Util.numBits(val)
        return count
    countBits = staticmethod(countBits)


################################################################################
#
#  XAPI
#
class XAPI:
    USER = "root"
    PLUGIN_ON_SLAVE = "on-slave"

    CONFIG_SM = 0
    CONFIG_OTHER = 1
    CONFIG_ON_BOOT = 2
    CONFIG_ALLOW_CACHING = 3

    CONFIG_NAME = {
            CONFIG_SM: "sm-config",
            CONFIG_OTHER: "other-config",
            CONFIG_ON_BOOT: "on-boot",
            CONFIG_ALLOW_CACHING: "allow_caching"
    }

    class LookupError(util.SMException):
        pass

    def getSession():
        session = XenAPI.xapi_local()
        session.xenapi.login_with_password(XAPI.USER, '', '', 'SM')
        return session
    getSession = staticmethod(getSession)

    def __init__(self, session, srUuid):
        self.sessionPrivate = False
        self.session = session
        if self.session is None:
            self.session = self.getSession()
            self.sessionPrivate = True
        self._srRef = self.session.xenapi.SR.get_by_uuid(srUuid)
        self.srRecord = self.session.xenapi.SR.get_record(self._srRef)
        self.hostUuid = util.get_this_host()
        self._hostRef = self.session.xenapi.host.get_by_uuid(self.hostUuid)
        self.task = None
        self.task_progress = {"coalescable": 0, "done": 0}

    def __del__(self):
        if self.sessionPrivate:
            self.session.xenapi.session.logout()

    @property
    def srRef(self):
        return self._srRef

    def isPluggedHere(self):
        pbds = self.getAttachedPBDs()
        for pbdRec in pbds:
            if pbdRec["host"] == self._hostRef:
                return True
        return False

    def poolOK(self):
        host_recs = self.session.xenapi.host.get_all_records()
        for host_ref, host_rec in host_recs.items():
            if not host_rec["enabled"]:
                Util.log("Host %s not enabled" % host_rec["uuid"])
                return False
        return True

    def isMaster(self):
        if self.srRecord["shared"]:
            pool = list(self.session.xenapi.pool.get_all_records().values())[0]
            return pool["master"] == self._hostRef
        else:
            pbds = self.getAttachedPBDs()
            if len(pbds) < 1:
                raise util.SMException("Local SR not attached")
            elif len(pbds) > 1:
                raise util.SMException("Local SR multiply attached")
            return pbds[0]["host"] == self._hostRef

    def getAttachedPBDs(self):
        """Return PBD records for all PBDs of this SR that are currently
        attached"""
        attachedPBDs = []
        pbds = self.session.xenapi.PBD.get_all_records()
        for pbdRec in pbds.values():
            if pbdRec["SR"] == self._srRef and pbdRec["currently_attached"]:
                attachedPBDs.append(pbdRec)
        return attachedPBDs

    def getOnlineHosts(self):
        return util.get_online_hosts(self.session)

    def ensureInactive(self, hostRef, args):
        text = self.session.xenapi.host.call_plugin( \
                hostRef, self.PLUGIN_ON_SLAVE, "multi", args)
        Util.log("call-plugin returned: '%s'" % text)

    def getRecordHost(self, hostRef):
        return self.session.xenapi.host.get_record(hostRef)

    def _getRefVDI(self, uuid):
        return self.session.xenapi.VDI.get_by_uuid(uuid)

    def getRefVDI(self, vdi):
        return self._getRefVDI(vdi.uuid)

    def getRecordVDI(self, uuid):
        try:
            ref = self._getRefVDI(uuid)
            return self.session.xenapi.VDI.get_record(ref)
        except XenAPI.Failure:
            return None

    def singleSnapshotVDI(self, vdi):
        return self.session.xenapi.VDI.snapshot(vdi.getRef(),
                {"type": "internal"})

    def forgetVDI(self, srUuid, vdiUuid):
        """Forget the VDI, but handle the case where the VDI has already been
        forgotten (i.e. ignore errors)"""
        try:
            vdiRef = self.session.xenapi.VDI.get_by_uuid(vdiUuid)
            self.session.xenapi.VDI.forget(vdiRef)
        except XenAPI.Failure:
            pass

    def getConfigVDI(self, vdi, key):
        kind = vdi.CONFIG_TYPE[key]
        if kind == self.CONFIG_SM:
            cfg = self.session.xenapi.VDI.get_sm_config(vdi.getRef())
        elif kind == self.CONFIG_OTHER:
            cfg = self.session.xenapi.VDI.get_other_config(vdi.getRef())
        elif kind == self.CONFIG_ON_BOOT:
            cfg = self.session.xenapi.VDI.get_on_boot(vdi.getRef())
        elif kind == self.CONFIG_ALLOW_CACHING:
            cfg = self.session.xenapi.VDI.get_allow_caching(vdi.getRef())
        else:
            assert(False)
        Util.log("Got %s for %s: %s" % (self.CONFIG_NAME[kind], vdi, repr(cfg)))
        return cfg

    def removeFromConfigVDI(self, vdi, key):
        kind = vdi.CONFIG_TYPE[key]
        if kind == self.CONFIG_SM:
            self.session.xenapi.VDI.remove_from_sm_config(vdi.getRef(), key)
        elif kind == self.CONFIG_OTHER:
            self.session.xenapi.VDI.remove_from_other_config(vdi.getRef(), key)
        else:
            assert(False)

    def addToConfigVDI(self, vdi, key, val):
        kind = vdi.CONFIG_TYPE[key]
        if kind == self.CONFIG_SM:
            self.session.xenapi.VDI.add_to_sm_config(vdi.getRef(), key, val)
        elif kind == self.CONFIG_OTHER:
            self.session.xenapi.VDI.add_to_other_config(vdi.getRef(), key, val)
        else:
            assert(False)

    def isSnapshot(self, vdi):
        return self.session.xenapi.VDI.get_is_a_snapshot(vdi.getRef())

    def markCacheSRsDirty(self):
        sr_refs = self.session.xenapi.SR.get_all_records_where( \
                'field "local_cache_enabled" = "true"')
        for sr_ref in sr_refs:
            Util.log("Marking SR %s dirty" % sr_ref)
            util.set_dirty(self.session, sr_ref)

    def srUpdate(self):
        Util.log("Starting asynch srUpdate for SR %s" % self.srRecord["uuid"])
        abortFlag = IPCFlag(self.srRecord["uuid"])
        task = self.session.xenapi.Async.SR.update(self._srRef)
        cancelTask = True
        try:
            for i in range(60):
                status = self.session.xenapi.task.get_status(task)
                if not status == "pending":
                    Util.log("SR.update_asynch status changed to [%s]" % status)
                    cancelTask = False
                    return
                if abortFlag.test(FLAG_TYPE_ABORT):
                    Util.log("Abort signalled during srUpdate, cancelling task...")
                    try:
                        self.session.xenapi.task.cancel(task)
                        cancelTask = False
                        Util.log("Task cancelled")
                    except:
                        pass
                    return
                time.sleep(1)
        finally:
            if cancelTask:
                self.session.xenapi.task.cancel(task)
            self.session.xenapi.task.destroy(task)
        Util.log("Asynch srUpdate still running, but timeout exceeded.")

    def update_task(self):
        self.session.xenapi.task.set_other_config(
            self.task,
            {
                "applies_to": self._srRef
            })
        total = self.task_progress['coalescable'] + self.task_progress['done']
        if (total > 0):
            self.session.xenapi.task.set_progress(
                self.task, float(self.task_progress['done']) / total)

    def create_task(self, label, description):
        self.task = self.session.xenapi.task.create(label, description)
        self.update_task()

    def update_task_progress(self, key, value):
        self.task_progress[key] = value
        if self.task:
            self.update_task()

    def set_task_status(self, status):
        if self.task:
            self.session.xenapi.task.set_status(self.task, status)


################################################################################
#
#  VDI
#
class VDI:
    """Object representing a VDI of a VHD-based SR"""

    POLL_INTERVAL = 1
    POLL_TIMEOUT = 30
    DEVICE_MAJOR = 202
    DRIVER_NAME_VHD = "vhd"

    # config keys & values
    DB_VHD_PARENT = "vhd-parent"
    DB_VDI_TYPE = "vdi_type"
    DB_VHD_BLOCKS = "vhd-blocks"
    DB_VDI_PAUSED = "paused"
    DB_VDI_RELINKING = "relinking"
    DB_VDI_ACTIVATING = "activating"
    DB_GC = "gc"
    DB_COALESCE = "coalesce"
    DB_LEAFCLSC = "leaf-coalesce"  # config key
    DB_GC_NO_SPACE = "gc_no_space"
    LEAFCLSC_DISABLED = "false"  # set by user; means do not leaf-coalesce
    LEAFCLSC_FORCE = "force"     # set by user; means skip snap-coalesce
    LEAFCLSC_OFFLINE = "offline"  # set here for informational purposes: means
    # no space to snap-coalesce or unable to keep
    # up with VDI. This is not used by the SM, it
    # might be used by external components.
    DB_ONBOOT = "on-boot"
    ONBOOT_RESET = "reset"
    DB_ALLOW_CACHING = "allow_caching"

    CONFIG_TYPE = {
            DB_VHD_PARENT: XAPI.CONFIG_SM,
            DB_VDI_TYPE: XAPI.CONFIG_SM,
            DB_VHD_BLOCKS: XAPI.CONFIG_SM,
            DB_VDI_PAUSED: XAPI.CONFIG_SM,
            DB_VDI_RELINKING: XAPI.CONFIG_SM,
            DB_VDI_ACTIVATING: XAPI.CONFIG_SM,
            DB_GC: XAPI.CONFIG_OTHER,
            DB_COALESCE: XAPI.CONFIG_OTHER,
            DB_LEAFCLSC: XAPI.CONFIG_OTHER,
            DB_ONBOOT: XAPI.CONFIG_ON_BOOT,
            DB_ALLOW_CACHING: XAPI.CONFIG_ALLOW_CACHING,
            DB_GC_NO_SPACE: XAPI.CONFIG_SM
    }

    LIVE_LEAF_COALESCE_MAX_SIZE = 20 * 1024 * 1024  # bytes
    LIVE_LEAF_COALESCE_TIMEOUT = 10  # seconds
    TIMEOUT_SAFETY_MARGIN = 0.5  # extra margin when calculating
    # feasibility of leaf coalesce

    JRN_RELINK = "relink"  # journal entry type for relinking children
    JRN_COALESCE = "coalesce"  # to communicate which VDI is being coalesced
    JRN_LEAF = "leaf"  # used in coalesce-leaf

    STR_TREE_INDENT = 4

    def __init__(self, sr, uuid, raw):
        self.sr = sr
        self.scanError = True
        self.uuid = uuid
        self.raw = raw
        self.fileName = ""
        self.parentUuid = ""
        self.sizeVirt = -1
        self._sizeVHD = -1
        self._sizeAllocated = -1
        self.hidden = False
        self.parent = None
        self.children = []
        self._vdiRef = None
        self._clearRef()

    @staticmethod
    def extractUuid(path):
        raise NotImplementedError("Implement in sub class")

    def load(self, info=None):
        """Load VDI info"""
        pass  # abstract

    def getDriverName(self):
        return self.DRIVER_NAME_VHD

    def getRef(self):
        if self._vdiRef is None:
            self._vdiRef = self.sr.xapi.getRefVDI(self)
        return self._vdiRef

    def getConfig(self, key, default=None):
        config = self.sr.xapi.getConfigVDI(self, key)
        if key == self.DB_ONBOOT or key == self.DB_ALLOW_CACHING:
            val = config
        else:
            val = config.get(key)
        if val:
            return val
        return default

    def setConfig(self, key, val):
        self.sr.xapi.removeFromConfigVDI(self, key)
        self.sr.xapi.addToConfigVDI(self, key, val)
        Util.log("Set %s = %s for %s" % (key, val, self))

    def delConfig(self, key):
        self.sr.xapi.removeFromConfigVDI(self, key)
        Util.log("Removed %s from %s" % (key, self))

    def ensureUnpaused(self):
        if self.getConfig(self.DB_VDI_PAUSED) == "true":
            Util.log("Unpausing VDI %s" % self)
            self.unpause()

    def pause(self, failfast=False):
        if not blktap2.VDI.tap_pause(self.sr.xapi.session, self.sr.uuid,
                self.uuid, failfast):
            raise util.SMException("Failed to pause VDI %s" % self)

    def _report_tapdisk_unpause_error(self):
        try:
            xapi = self.sr.xapi.session.xenapi
            sr_ref = xapi.SR.get_by_uuid(self.sr.uuid)
            msg_name = "failed to unpause tapdisk"
            msg_body = "Failed to unpause tapdisk for VDI %s, " \
                    "VMs using this tapdisk have lost access " \
                    "to the corresponding disk(s)" % self.uuid
            xapi.message.create(msg_name, "4", "SR", self.sr.uuid, msg_body)
        except Exception as e:
            util.SMlog("failed to generate message: %s" % e)

    def unpause(self):
        if not blktap2.VDI.tap_unpause(self.sr.xapi.session, self.sr.uuid,
                self.uuid):
            self._report_tapdisk_unpause_error()
            raise util.SMException("Failed to unpause VDI %s" % self)

    def refresh(self, ignoreNonexistent=True):
        """Pause-unpause in one step"""
        self.sr.lock()
        try:
            try:
                if not blktap2.VDI.tap_refresh(self.sr.xapi.session,
                        self.sr.uuid, self.uuid):
                    self._report_tapdisk_unpause_error()
                    raise util.SMException("Failed to refresh %s" % self)
            except XenAPI.Failure as e:
                if util.isInvalidVDI(e) and ignoreNonexistent:
                    Util.log("VDI %s not found, ignoring" % self)
                    return
                raise
        finally:
            self.sr.unlock()

    def isSnapshot(self):
        return self.sr.xapi.isSnapshot(self)

    def isAttachedRW(self):
        return util.is_attached_rw(
                self.sr.xapi.session.xenapi.VDI.get_sm_config(self.getRef()))

    def getVHDBlocks(self):
        val = self.updateBlockInfo()
        bitmap = zlib.decompress(base64.b64decode(val))
        return bitmap

    def isCoalesceable(self):
        """A VDI is coalesceable if it has no siblings and is not a leaf"""
        return not self.scanError and \
                self.parent and \
                len(self.parent.children) == 1 and \
                self.hidden and \
                len(self.children) > 0

    def isLeafCoalesceable(self):
        """A VDI is leaf-coalesceable if it has no siblings and is a leaf"""
        return not self.scanError and \
                self.parent and \
                len(self.parent.children) == 1 and \
                not self.hidden and \
                len(self.children) == 0

    def canLiveCoalesce(self, speed):
        """Can we stop-and-leaf-coalesce this VDI? The VDI must be
        isLeafCoalesceable() already"""
        feasibleSize = False
        allowedDownTime = \
                self.TIMEOUT_SAFETY_MARGIN * self.LIVE_LEAF_COALESCE_TIMEOUT
        vhd_size = self.getAllocatedSize()
        if speed:
            feasibleSize = \
                vhd_size // speed < allowedDownTime
        else:
            feasibleSize = \
                vhd_size < self.LIVE_LEAF_COALESCE_MAX_SIZE

        return (feasibleSize or
                self.getConfig(self.DB_LEAFCLSC) == self.LEAFCLSC_FORCE)

    def getAllPrunable(self):
        if len(self.children) == 0:  # base case
            # it is possible to have a hidden leaf that was recently coalesced
            # onto its parent, its children already relinked but not yet
            # reloaded - in which case it may not be garbage collected yet:
            # some tapdisks could still be using the file.
            if self.sr.journaler.get(self.JRN_RELINK, self.uuid):
                return []
            if not self.scanError and self.hidden:
                return [self]
            return []

        thisPrunable = True
        vdiList = []
        for child in self.children:
            childList = child.getAllPrunable()
            vdiList.extend(childList)
            if child not in childList:
                thisPrunable = False

        if not self.scanError and thisPrunable:
            vdiList.append(self)
        return vdiList

    def getSizeVHD(self):
        return self._sizeVHD

    def getAllocatedSize(self):
        return self._sizeAllocated

    def getTreeRoot(self):
        "Get the root of the tree that self belongs to"
        root = self
        while root.parent:
            root = root.parent
        return root

    def getTreeHeight(self):
        "Get the height of the subtree rooted at self"
        if len(self.children) == 0:
            return 1

        maxChildHeight = 0
        for child in self.children:
            childHeight = child.getTreeHeight()
            if childHeight > maxChildHeight:
                maxChildHeight = childHeight

        return maxChildHeight + 1

    def getAllLeaves(self):
        "Get all leaf nodes in the subtree rooted at self"
        if len(self.children) == 0:
            return [self]

        leaves = []
        for child in self.children:
            leaves.extend(child.getAllLeaves())
        return leaves

    def updateBlockInfo(self):
        val = base64.b64encode(self._queryVHDBlocks()).decode()
        self.setConfig(VDI.DB_VHD_BLOCKS, val)
        return val

    def rename(self, uuid):
        "Rename the VDI file"
        assert(not self.sr.vdis.get(uuid))
        self._clearRef()
        oldUuid = self.uuid
        self.uuid = uuid
        self.children = []
        # updating the children themselves is the responsibility of the caller
        del self.sr.vdis[oldUuid]
        self.sr.vdis[self.uuid] = self

    def delete(self):
        "Physically delete the VDI"
        lock.Lock.cleanup(self.uuid, lvhdutil.NS_PREFIX_LVM + self.sr.uuid)
        lock.Lock.cleanupAll(self.uuid)
        self._clear()

    def __str__(self):
        strHidden = ""
        if self.hidden:
            strHidden = "*"
        strSizeVirt = "?"
        if self.sizeVirt > 0:
            strSizeVirt = Util.num2str(self.sizeVirt)
        strSizeVHD = "?"
        if self._sizeVHD > 0:
            strSizeVHD = "/%s" % Util.num2str(self._sizeVHD)
        strSizeAllocated = "?"
        if self._sizeAllocated >= 0:
            strSizeAllocated = "/%s" % Util.num2str(self._sizeAllocated)
        strType = ""
        if self.raw:
            strType = "[RAW]"
            strSizeVHD = ""

        return "%s%s(%s%s%s)%s" % (strHidden, self.uuid[0:8], strSizeVirt,
                strSizeVHD, strSizeAllocated, strType)

    def validate(self, fast=False):
        if not vhdutil.check(self.path, fast=fast):
            raise util.SMException("VHD %s corrupted" % self)

    def _clear(self):
        self.uuid = ""
        self.path = ""
        self.parentUuid = ""
        self.parent = None
        self._clearRef()

    def _clearRef(self):
        self._vdiRef = None

    def _doCoalesce(self):
        """Coalesce self onto parent. Only perform the actual coalescing of
        VHD, but not the subsequent relinking. We'll do that as the next step,
        after reloading the entire SR in case things have changed while we
        were coalescing"""
        self.validate()
        self.parent.validate(True)
        self.parent._increaseSizeVirt(self.sizeVirt)
        self.sr._updateSlavesOnResize(self.parent)
        self._coalesceVHD(0)
        self.parent.validate(True)
        #self._verifyContents(0)
        self.parent.updateBlockInfo()

    def _verifyContents(self, timeOut):
        Util.log("  Coalesce verification on %s" % self)
        abortTest = lambda: IPCFlag(self.sr.uuid).test(FLAG_TYPE_ABORT)
        Util.runAbortable(lambda: self._runTapdiskDiff(), True,
                self.sr.uuid, abortTest, VDI.POLL_INTERVAL, timeOut)
        Util.log("  Coalesce verification succeeded")

    def _runTapdiskDiff(self):
        cmd = "tapdisk-diff -n %s:%s -m %s:%s" % \
                (self.getDriverName(), self.path, \
                self.parent.getDriverName(), self.parent.path)
        Util.doexec(cmd, 0)
        return True

    def _reportCoalesceError(vdi, ce):
        """Reports a coalesce error to XenCenter.

        vdi: the VDI object on which the coalesce error occured
        ce: the CommandException that was raised"""

        msg_name = os.strerror(ce.code)
        if ce.code == errno.ENOSPC:
            # TODO We could add more information here, e.g. exactly how much
            # space is required for the particular coalesce, as well as actions
            # to be taken by the user and consequences of not taking these
            # actions.
            msg_body = 'Run out of space while coalescing.'
        elif ce.code == errno.EIO:
            msg_body = 'I/O error while coalescing.'
        else:
            msg_body = ''
        util.SMlog('Coalesce failed on SR %s: %s (%s)'
                % (vdi.sr.uuid, msg_name, msg_body))

        # Create a XenCenter message, but don't spam.
        xapi = vdi.sr.xapi.session.xenapi
        sr_ref = xapi.SR.get_by_uuid(vdi.sr.uuid)
        oth_cfg = xapi.SR.get_other_config(sr_ref)
        if COALESCE_ERR_RATE_TAG in oth_cfg:
            coalesce_err_rate = float(oth_cfg[COALESCE_ERR_RATE_TAG])
        else:
            coalesce_err_rate = DEFAULT_COALESCE_ERR_RATE

        xcmsg = False
        if coalesce_err_rate == 0:
            xcmsg = True
        elif coalesce_err_rate > 0:
            now = datetime.datetime.now()
            sm_cfg = xapi.SR.get_sm_config(sr_ref)
            if COALESCE_LAST_ERR_TAG in sm_cfg:
                # seconds per message (minimum distance in time between two
                # messages in seconds)
                spm = datetime.timedelta(seconds=(1.0 / coalesce_err_rate) * 60)
                last = datetime.datetime.fromtimestamp(
                        float(sm_cfg[COALESCE_LAST_ERR_TAG]))
                if now - last >= spm:
                    xapi.SR.remove_from_sm_config(sr_ref,
                            COALESCE_LAST_ERR_TAG)
                    xcmsg = True
            else:
                xcmsg = True
            if xcmsg:
                xapi.SR.add_to_sm_config(sr_ref, COALESCE_LAST_ERR_TAG,
                        str(now.strftime('%s')))
        if xcmsg:
            xapi.message.create(msg_name, "3", "SR", vdi.sr.uuid, msg_body)
    _reportCoalesceError = staticmethod(_reportCoalesceError)

    def _doCoalesceVHD(vdi):
        try:
            startTime = time.time()
            vhdSize = vdi.getAllocatedSize()
            # size is returned in sectors
            coalesced_size = vhdutil.coalesce(vdi.path) * 512
            endTime = time.time()
            vdi.sr.recordStorageSpeed(startTime, endTime, coalesced_size)
        except util.CommandException as ce:
            # We use try/except for the following piece of code because it runs
            # in a separate process context and errors will not be caught and
            # reported by anyone.
            try:
                # Report coalesce errors back to user via XC
                VDI._reportCoalesceError(vdi, ce)
            except Exception as e:
                util.SMlog('failed to create XenCenter message: %s' % e)
            raise ce
        except:
            raise
    _doCoalesceVHD = staticmethod(_doCoalesceVHD)

    def _vdi_is_raw(self, vdi_path):
        """
        Given path to vdi determine if it is raw
        """
        uuid = self.extractUuid(vdi_path)
        return self.sr.vdis[uuid].raw

    def _coalesceVHD(self, timeOut):
        Util.log("  Running VHD coalesce on %s" % self)
        abortTest = lambda: IPCFlag(self.sr.uuid).test(FLAG_TYPE_ABORT)
        try:
            util.fistpoint.activate_custom_fn(
                "cleanup_coalesceVHD_inject_failure",
                util.inject_failure)
            Util.runAbortable(lambda: VDI._doCoalesceVHD(self), None,
                    self.sr.uuid, abortTest, VDI.POLL_INTERVAL, timeOut)
        except:
            #exception at this phase could indicate a failure in vhd coalesce
            # or a kill of vhd coalesce by runAbortable due to  timeOut
            # Try a repair and reraise the exception
            parent = ""
            try:
                parent = vhdutil.getParent(self.path, lambda x: x.strip())
                if not self._vdi_is_raw(parent):
                    # Repair error is logged and ignored. Error reraised later
                    util.SMlog('Coalesce failed on %s, attempting repair on ' \
                               'parent %s' % (self.uuid, parent))
                    vhdutil.repair(parent)
            except Exception as e:
                util.SMlog('(error ignored) Failed to repair parent %s ' \
                           'after failed coalesce on %s, err: %s' %
                           (parent, self.path, e))
            raise

        util.fistpoint.activate("LVHDRT_coalescing_VHD_data", self.sr.uuid)

    def _relinkSkip(self):
        """Relink children of this VDI to point to the parent of this VDI"""
        abortFlag = IPCFlag(self.sr.uuid)
        for child in self.children:
            if abortFlag.test(FLAG_TYPE_ABORT):
                raise AbortException("Aborting due to signal")
            Util.log("  Relinking %s from %s to %s" % \
                    (child, self, self.parent))
            util.fistpoint.activate("LVHDRT_relinking_grandchildren", self.sr.uuid)
            child._setParent(self.parent)
        self.children = []

    def _reloadChildren(self, vdiSkip):
        """Pause & unpause all VDIs in the subtree to cause blktap to reload
        the VHD metadata for this file in any online VDI"""
        abortFlag = IPCFlag(self.sr.uuid)
        for child in self.children:
            if child == vdiSkip:
                continue
            if abortFlag.test(FLAG_TYPE_ABORT):
                raise AbortException("Aborting due to signal")
            Util.log("  Reloading VDI %s" % child)
            child._reload()

    def _reload(self):
        """Pause & unpause to cause blktap to reload the VHD metadata"""
        for child in self.children:
            child._reload()

        # only leaves can be attached
        if len(self.children) == 0:
            try:
                self.delConfig(VDI.DB_VDI_RELINKING)
            except XenAPI.Failure as e:
                if not util.isInvalidVDI(e):
                    raise
            self.refresh()

    def _tagChildrenForRelink(self):
        if len(self.children) == 0:
            retries = 0
            try:
                while retries < 15:
                    retries += 1
                    if self.getConfig(VDI.DB_VDI_ACTIVATING) is not None:
                        Util.log("VDI %s is activating, wait to relink" %
                                 self.uuid)
                    else:
                        self.setConfig(VDI.DB_VDI_RELINKING, "True")

                        if self.getConfig(VDI.DB_VDI_ACTIVATING):
                            self.delConfig(VDI.DB_VDI_RELINKING)
                            Util.log("VDI %s started activating while tagging" %
                                     self.uuid)
                        else:
                            return
                    time.sleep(2)

                raise util.SMException("Failed to tag vdi %s for relink" % self)
            except XenAPI.Failure as e:
                if not util.isInvalidVDI(e):
                    raise

        for child in self.children:
            child._tagChildrenForRelink()

    def _loadInfoParent(self):
        ret = vhdutil.getParent(self.path, lvhdutil.extractUuid)
        if ret:
            self.parentUuid = ret

    def _setParent(self, parent):
        vhdutil.setParent(self.path, parent.path, False)
        self.parent = parent
        self.parentUuid = parent.uuid
        parent.children.append(self)
        try:
            self.setConfig(self.DB_VHD_PARENT, self.parentUuid)
            Util.log("Updated the vhd-parent field for child %s with %s" % \
                     (self.uuid, self.parentUuid))
        except:
            Util.log("Failed to update %s with vhd-parent field %s" % \
                     (self.uuid, self.parentUuid))

    def _loadInfoHidden(self):
        hidden = vhdutil.getHidden(self.path)
        self.hidden = (hidden != 0)

    def _setHidden(self, hidden=True):
        vhdutil.setHidden(self.path, hidden)
        self.hidden = hidden

    def _increaseSizeVirt(self, size, atomic=True):
        """ensure the virtual size of 'self' is at least 'size'. Note that
        resizing a VHD must always be offline and atomically: the file must
        not be open by anyone and no concurrent operations may take place.
        Thus we use the Agent API call for performing paused atomic
        operations. If the caller is already in the atomic context, it must
        call with atomic = False"""
        if self.sizeVirt >= size:
            return
        Util.log("  Expanding VHD virt size for VDI %s: %s -> %s" % \
                (self, Util.num2str(self.sizeVirt), Util.num2str(size)))

        msize = vhdutil.getMaxResizeSize(self.path) * 1024 * 1024
        if (size <= msize):
            vhdutil.setSizeVirtFast(self.path, size)
        else:
            if atomic:
                vdiList = self._getAllSubtree()
                self.sr.lock()
                try:
                    self.sr.pauseVDIs(vdiList)
                    try:
                        self._setSizeVirt(size)
                    finally:
                        self.sr.unpauseVDIs(vdiList)
                finally:
                    self.sr.unlock()
            else:
                self._setSizeVirt(size)

        self.sizeVirt = vhdutil.getSizeVirt(self.path)

    def _setSizeVirt(self, size):
        """WARNING: do not call this method directly unless all VDIs in the
        subtree are guaranteed to be unplugged (and remain so for the duration
        of the operation): this operation is only safe for offline VHDs"""
        jFile = os.path.join(self.sr.path, self.uuid)
        vhdutil.setSizeVirt(self.path, size, jFile)

    def _queryVHDBlocks(self):
        return vhdutil.getBlockBitmap(self.path)

    def _getCoalescedSizeData(self):
        """Get the data size of the resulting VHD if we coalesce self onto
        parent. We calculate the actual size by using the VHD block allocation
        information (as opposed to just adding up the two VHD sizes to get an
        upper bound)"""
        # make sure we don't use stale BAT info from vdi_rec since the child
        # was writable all this time
        self.delConfig(VDI.DB_VHD_BLOCKS)
        blocksChild = self.getVHDBlocks()
        blocksParent = self.parent.getVHDBlocks()
        numBlocks = Util.countBits(blocksChild, blocksParent)
        Util.log("Num combined blocks = %d" % numBlocks)
        sizeData = numBlocks * vhdutil.VHD_BLOCK_SIZE
        assert(sizeData <= self.sizeVirt)
        return sizeData

    def _calcExtraSpaceForCoalescing(self):
        sizeData = self._getCoalescedSizeData()
        sizeCoalesced = sizeData + vhdutil.calcOverheadBitmap(sizeData) + \
                vhdutil.calcOverheadEmpty(self.sizeVirt)
        Util.log("Coalesced size = %s" % Util.num2str(sizeCoalesced))
        return sizeCoalesced - self.parent.getSizeVHD()

    def _calcExtraSpaceForLeafCoalescing(self):
        """How much extra space in the SR will be required to
        [live-]leaf-coalesce this VDI"""
        # the space requirements are the same as for inline coalesce
        return self._calcExtraSpaceForCoalescing()

    def _calcExtraSpaceForSnapshotCoalescing(self):
        """How much extra space in the SR will be required to
        snapshot-coalesce this VDI"""
        return self._calcExtraSpaceForCoalescing() + \
                vhdutil.calcOverheadEmpty(self.sizeVirt)  # extra snap leaf

    def _getAllSubtree(self):
        """Get self and all VDIs in the subtree of self as a flat list"""
        vdiList = [self]
        for child in self.children:
            vdiList.extend(child._getAllSubtree())
        return vdiList


class FileVDI(VDI):
    """Object representing a VDI in a file-based SR (EXT or NFS)"""

    @staticmethod
    def extractUuid(path):
        path = os.path.basename(path.strip())
        if not (path.endswith(vhdutil.FILE_EXTN_VHD) or \
                path.endswith(vhdutil.FILE_EXTN_RAW)):
            return None
        uuid = path.replace(vhdutil.FILE_EXTN_VHD, "").replace( \
                vhdutil.FILE_EXTN_RAW, "")
        # TODO: validate UUID format
        return uuid

    def __init__(self, sr, uuid, raw):
        VDI.__init__(self, sr, uuid, raw)
        if self.raw:
            self.fileName = "%s%s" % (self.uuid, vhdutil.FILE_EXTN_RAW)
        else:
            self.fileName = "%s%s" % (self.uuid, vhdutil.FILE_EXTN_VHD)

    def load(self, info=None):
        if not info:
            if not util.pathexists(self.path):
                raise util.SMException("%s not found" % self.path)
            try:
                info = vhdutil.getVHDInfo(self.path, self.extractUuid)
            except util.SMException:
                Util.log(" [VDI %s: failed to read VHD metadata]" % self.uuid)
                return
        self.parent = None
        self.children = []
        self.parentUuid = info.parentUuid
        self.sizeVirt = info.sizeVirt
        self._sizeVHD = info.sizePhys
        self._sizeAllocated = info.sizeAllocated
        self.hidden = info.hidden
        self.scanError = False
        self.path = os.path.join(self.sr.path, "%s%s" % \
                (self.uuid, vhdutil.FILE_EXTN_VHD))

    def rename(self, uuid):
        oldPath = self.path
        VDI.rename(self, uuid)
        self.fileName = "%s%s" % (self.uuid, vhdutil.FILE_EXTN_VHD)
        self.path = os.path.join(self.sr.path, self.fileName)
        assert(not util.pathexists(self.path))
        Util.log("Renaming %s -> %s" % (oldPath, self.path))
        os.rename(oldPath, self.path)

    def delete(self):
        if len(self.children) > 0:
            raise util.SMException("VDI %s has children, can't delete" % \
                    self.uuid)
        try:
            self.sr.lock()
            try:
                os.unlink(self.path)
                self.sr.forgetVDI(self.uuid)
            finally:
                self.sr.unlock()
        except OSError:
            raise util.SMException("os.unlink(%s) failed" % self.path)
        VDI.delete(self)

    def getAllocatedSize(self):
        if self._sizeAllocated == -1:
            self._sizeAllocated = vhdutil.getAllocatedSize(self.path)
        return self._sizeAllocated


class LVHDVDI(VDI):
    """Object representing a VDI in an LVHD SR"""

    JRN_ZERO = "zero"  # journal entry type for zeroing out end of parent
    DRIVER_NAME_RAW = "aio"

    def load(self, vdiInfo):
        self.parent = None
        self.children = []
        self._sizeVHD = -1
        self._sizeAllocated = -1
        self.scanError = vdiInfo.scanError
        self.sizeLV = vdiInfo.sizeLV
        self.sizeVirt = vdiInfo.sizeVirt
        self.fileName = vdiInfo.lvName
        self.lvActive = vdiInfo.lvActive
        self.lvOpen = vdiInfo.lvOpen
        self.lvReadonly = vdiInfo.lvReadonly
        self.hidden = vdiInfo.hidden
        self.parentUuid = vdiInfo.parentUuid
        self.path = os.path.join(self.sr.path, self.fileName)

    @staticmethod
    def extractUuid(path):
        return lvhdutil.extractUuid(path)

    def getDriverName(self):
        if self.raw:
            return self.DRIVER_NAME_RAW
        return self.DRIVER_NAME_VHD

    def inflate(self, size):
        """inflate the LV containing the VHD to 'size'"""
        if self.raw:
            return
        self._activate()
        self.sr.lock()
        try:
            lvhdutil.inflate(self.sr.journaler, self.sr.uuid, self.uuid, size)
            util.fistpoint.activate("LVHDRT_inflating_the_parent", self.sr.uuid)
        finally:
            self.sr.unlock()
        self.sizeLV = self.sr.lvmCache.getSize(self.fileName)
        self._sizeVHD = -1
        self._sizeAllocated = -1

    def deflate(self):
        """deflate the LV containing the VHD to minimum"""
        if self.raw:
            return
        self._activate()
        self.sr.lock()
        try:
            lvhdutil.deflate(self.sr.lvmCache, self.fileName, self.getSizeVHD())
        finally:
            self.sr.unlock()
        self.sizeLV = self.sr.lvmCache.getSize(self.fileName)
        self._sizeVHD = -1
        self._sizeAllocated = -1

    def inflateFully(self):
        self.inflate(lvhdutil.calcSizeVHDLV(self.sizeVirt))

    def inflateParentForCoalesce(self):
        """Inflate the parent only as much as needed for the purposes of
        coalescing"""
        if self.parent.raw:
            return
        inc = self._calcExtraSpaceForCoalescing()
        if inc > 0:
            util.fistpoint.activate("LVHDRT_coalescing_before_inflate_grandparent", self.sr.uuid)
            self.parent.inflate(self.parent.sizeLV + inc)

    def updateBlockInfo(self):
        if not self.raw:
            return VDI.updateBlockInfo(self)

    def rename(self, uuid):
        oldUuid = self.uuid
        oldLVName = self.fileName
        VDI.rename(self, uuid)
        self.fileName = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_VHD] + self.uuid
        if self.raw:
            self.fileName = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_RAW] + self.uuid
        self.path = os.path.join(self.sr.path, self.fileName)
        assert(not self.sr.lvmCache.checkLV(self.fileName))

        self.sr.lvmCache.rename(oldLVName, self.fileName)
        if self.sr.lvActivator.get(oldUuid, False):
            self.sr.lvActivator.replace(oldUuid, self.uuid, self.fileName, False)

        ns = lvhdutil.NS_PREFIX_LVM + self.sr.uuid
        (cnt, bcnt) = RefCounter.check(oldUuid, ns)
        RefCounter.set(self.uuid, cnt, bcnt, ns)
        RefCounter.reset(oldUuid, ns)

    def delete(self):
        if len(self.children) > 0:
            raise util.SMException("VDI %s has children, can't delete" % \
                    self.uuid)
        self.sr.lock()
        try:
            self.sr.lvmCache.remove(self.fileName)
            self.sr.forgetVDI(self.uuid)
        finally:
            self.sr.unlock()
        RefCounter.reset(self.uuid, lvhdutil.NS_PREFIX_LVM + self.sr.uuid)
        VDI.delete(self)

    def getSizeVHD(self):
        if self._sizeVHD == -1:
            self._loadInfoSizeVHD()
        return self._sizeVHD

    def _loadInfoSizeVHD(self):
        """Get the physical utilization of the VHD file. We do it individually
        (and not using the VHD batch scanner) as an optimization: this info is
        relatively expensive and we need it only for VDI's involved in
        coalescing."""
        if self.raw:
            return
        self._activate()
        self._sizeVHD = vhdutil.getSizePhys(self.path)
        if self._sizeVHD <= 0:
            raise util.SMException("phys size of %s = %d" % \
                    (self, self._sizeVHD))

    def getAllocatedSize(self):
        if self._sizeAllocated == -1:
            self._loadInfoSizeAllocated()
        return self._sizeAllocated

    def _loadInfoSizeAllocated(self):
        """
        Get the allocated size of the VHD volume.
        """
        if self.raw:
            return
        self._activate()
        self._sizeAllocated = vhdutil.getAllocatedSize(self.path)

    def _loadInfoHidden(self):
        if self.raw:
            self.hidden = self.sr.lvmCache.getHidden(self.fileName)
        else:
            VDI._loadInfoHidden(self)

    def _setHidden(self, hidden=True):
        if self.raw:
            self.sr.lvmCache.setHidden(self.fileName, hidden)
            self.hidden = hidden
        else:
            VDI._setHidden(self, hidden)

    def __str__(self):
        strType = "VHD"
        if self.raw:
            strType = "RAW"
        strHidden = ""
        if self.hidden:
            strHidden = "*"
        strSizeVHD = ""
        if self._sizeVHD > 0:
            strSizeVHD = Util.num2str(self._sizeVHD)
        strSizeAllocated = ""
        if self._sizeAllocated >= 0:
            strSizeAllocated = Util.num2str(self._sizeAllocated)
        strActive = "n"
        if self.lvActive:
            strActive = "a"
        if self.lvOpen:
            strActive += "o"
        return "%s%s[%s](%s/%s/%s/%s|%s)" % (strHidden, self.uuid[0:8], strType,
                Util.num2str(self.sizeVirt), strSizeVHD, strSizeAllocated,
                Util.num2str(self.sizeLV), strActive)

    def validate(self, fast=False):
        if not self.raw:
            VDI.validate(self, fast)

    def _doCoalesce(self):
        """LVHD parents must first be activated, inflated, and made writable"""
        try:
            self._activateChain()
            self.sr.lvmCache.setReadonly(self.parent.fileName, False)
            self.parent.validate()
            self.inflateParentForCoalesce()
            VDI._doCoalesce(self)
        finally:
            self.parent._loadInfoSizeVHD()
            self.parent.deflate()
            self.sr.lvmCache.setReadonly(self.parent.fileName, True)

    def _setParent(self, parent):
        self._activate()
        if self.lvReadonly:
            self.sr.lvmCache.setReadonly(self.fileName, False)

        try:
            vhdutil.setParent(self.path, parent.path, parent.raw)
        finally:
            if self.lvReadonly:
                self.sr.lvmCache.setReadonly(self.fileName, True)
        self._deactivate()
        self.parent = parent
        self.parentUuid = parent.uuid
        parent.children.append(self)
        try:
            self.setConfig(self.DB_VHD_PARENT, self.parentUuid)
            Util.log("Updated the vhd-parent field for child %s with %s" % \
                     (self.uuid, self.parentUuid))
        except:
            Util.log("Failed to update the vhd-parent with %s for child %s" % \
                     (self.parentUuid, self.uuid))

    def _activate(self):
        self.sr.lvActivator.activate(self.uuid, self.fileName, False)

    def _activateChain(self):
        vdi = self
        while vdi:
            vdi._activate()
            vdi = vdi.parent

    def _deactivate(self):
        self.sr.lvActivator.deactivate(self.uuid, False)

    def _increaseSizeVirt(self, size, atomic=True):
        "ensure the virtual size of 'self' is at least 'size'"
        self._activate()
        if not self.raw:
            VDI._increaseSizeVirt(self, size, atomic)
            return

        # raw VDI case
        offset = self.sizeLV
        if self.sizeVirt < size:
            oldSize = self.sizeLV
            self.sizeLV = util.roundup(lvutil.LVM_SIZE_INCREMENT, size)
            Util.log("  Growing %s: %d->%d" % (self.path, oldSize, self.sizeLV))
            self.sr.lvmCache.setSize(self.fileName, self.sizeLV)
            offset = oldSize
        unfinishedZero = False
        jval = self.sr.journaler.get(self.JRN_ZERO, self.uuid)
        if jval:
            unfinishedZero = True
            offset = int(jval)
        length = self.sizeLV - offset
        if not length:
            return

        if unfinishedZero:
            Util.log("  ==> Redoing unfinished zeroing out")
        else:
            self.sr.journaler.create(self.JRN_ZERO, self.uuid, \
                    str(offset))
        Util.log("  Zeroing %s: from %d, %dB" % (self.path, offset, length))
        abortTest = lambda: IPCFlag(self.sr.uuid).test(FLAG_TYPE_ABORT)
        func = lambda: util.zeroOut(self.path, offset, length)
        Util.runAbortable(func, True, self.sr.uuid, abortTest,
                VDI.POLL_INTERVAL, 0)
        self.sr.journaler.remove(self.JRN_ZERO, self.uuid)

    def _setSizeVirt(self, size):
        """WARNING: do not call this method directly unless all VDIs in the
        subtree are guaranteed to be unplugged (and remain so for the duration
        of the operation): this operation is only safe for offline VHDs"""
        self._activate()
        jFile = lvhdutil.createVHDJournalLV(self.sr.lvmCache, self.uuid,
                vhdutil.MAX_VHD_JOURNAL_SIZE)
        try:
            lvhdutil.setSizeVirt(self.sr.journaler, self.sr.uuid, self.uuid,
                    size, jFile)
        finally:
            lvhdutil.deleteVHDJournalLV(self.sr.lvmCache, self.uuid)

    def _queryVHDBlocks(self):
        self._activate()
        return VDI._queryVHDBlocks(self)

    def _calcExtraSpaceForCoalescing(self):
        if self.parent.raw:
            return 0  # raw parents are never deflated in the first place
        sizeCoalesced = lvhdutil.calcSizeVHDLV(self._getCoalescedSizeData())
        Util.log("Coalesced size = %s" % Util.num2str(sizeCoalesced))
        return sizeCoalesced - self.parent.sizeLV

    def _calcExtraSpaceForLeafCoalescing(self):
        """How much extra space in the SR will be required to
        [live-]leaf-coalesce this VDI"""
        # we can deflate the leaf to minimize the space requirements
        deflateDiff = self.sizeLV - lvhdutil.calcSizeLV(self.getSizeVHD())
        return self._calcExtraSpaceForCoalescing() - deflateDiff

    def _calcExtraSpaceForSnapshotCoalescing(self):
        return self._calcExtraSpaceForCoalescing() + \
                lvhdutil.calcSizeLV(self.getSizeVHD())


################################################################################
#
# SR
#
class SR:
    class LogFilter:
        def __init__(self, sr):
            self.sr = sr
            self.stateLogged = False
            self.prevState = {}
            self.currState = {}

        def logState(self):
            changes = ""
            self.currState.clear()
            for vdi in self.sr.vdiTrees:
                self.currState[vdi.uuid] = self._getTreeStr(vdi)
                if not self.prevState.get(vdi.uuid) or \
                        self.prevState[vdi.uuid] != self.currState[vdi.uuid]:
                    changes += self.currState[vdi.uuid]

            for uuid in self.prevState:
                if not self.currState.get(uuid):
                    changes += "Tree %s gone\n" % uuid

            result = "SR %s (%d VDIs in %d VHD trees): " % \
                    (self.sr, len(self.sr.vdis), len(self.sr.vdiTrees))

            if len(changes) > 0:
                if self.stateLogged:
                    result += "showing only VHD trees that changed:"
                result += "\n%s" % changes
            else:
                result += "no changes"

            for line in result.split("\n"):
                Util.log("%s" % line)
            self.prevState.clear()
            for key, val in self.currState.items():
                self.prevState[key] = val
            self.stateLogged = True

        def logNewVDI(self, uuid):
            if self.stateLogged:
                Util.log("Found new VDI when scanning: %s" % uuid)

        def _getTreeStr(self, vdi, indent=8):
            treeStr = "%s%s\n" % (" " * indent, vdi)
            for child in vdi.children:
                treeStr += self._getTreeStr(child, indent + VDI.STR_TREE_INDENT)
            return treeStr

    TYPE_FILE = "file"
    TYPE_LVHD = "lvhd"
    TYPES = [TYPE_LVHD, TYPE_FILE]

    LOCK_RETRY_INTERVAL = 3
    LOCK_RETRY_ATTEMPTS = 20
    LOCK_RETRY_ATTEMPTS_LOCK = 100

    SCAN_RETRY_ATTEMPTS = 3

    JRN_CLONE = "clone"  # journal entry type for the clone operation (from SM)
    TMP_RENAME_PREFIX = "OLD_"

    KEY_OFFLINE_COALESCE_NEEDED = "leaf_coalesce_need_offline"
    KEY_OFFLINE_COALESCE_OVERRIDE = "leaf_coalesce_offline_override"

    def getInstance(uuid, xapiSession, createLock=True, force=False):
        xapi = XAPI(xapiSession, uuid)
        type = normalizeType(xapi.srRecord["type"])
        if type == SR.TYPE_FILE:
            return FileSR(uuid, xapi, createLock, force)
        elif type == SR.TYPE_LVHD:
            return LVHDSR(uuid, xapi, createLock, force)
        raise util.SMException("SR type %s not recognized" % type)
    getInstance = staticmethod(getInstance)

    def __init__(self, uuid, xapi, createLock, force):
        self.logFilter = self.LogFilter(self)
        self.uuid = uuid
        self.path = ""
        self.name = ""
        self.vdis = {}
        self.vdiTrees = []
        self.journaler = None
        self.xapi = xapi
        self._locked = 0
        self._srLock = None
        if createLock:
            self._srLock = lock.Lock(vhdutil.LOCK_TYPE_SR, self.uuid)
        else:
            Util.log("Requested no SR locking")
        self.name = self.xapi.srRecord["name_label"]
        self._failedCoalesceTargets = []

        if not self.xapi.isPluggedHere():
            if force:
                Util.log("SR %s not attached on this host, ignoring" % uuid)
            else:
                if not self.wait_for_plug():
                    raise util.SMException("SR %s not attached on this host" % uuid)

        if force:
            Util.log("Not checking if we are Master (SR %s)" % uuid)
        elif not self.xapi.isMaster():
            raise util.SMException("This host is NOT master, will not run")

        self.no_space_candidates = {}

    def msg_cleared(self, xapi_session, msg_ref):
        try:
            msg = xapi_session.xenapi.message.get_record(msg_ref)
        except XenAPI.Failure:
            return True

        return msg is None

    def check_no_space_candidates(self):
        xapi_session = self.xapi.getSession()

        msg_id = self.xapi.srRecord["sm_config"].get(VDI.DB_GC_NO_SPACE)
        if self.no_space_candidates:
            if msg_id is None or self.msg_cleared(xapi_session, msg_id):
                util.SMlog("Could not coalesce due to a lack of space "
                           f"in SR {self.uuid}")
                msg_body = ("Unable to perform data coalesce due to a lack "
                            f"of space in SR {self.uuid}")
                msg_id = xapi_session.xenapi.message.create(
                    'SM_GC_NO_SPACE',
                    3,
                    "SR",
                    self.uuid,
                    msg_body)
                xapi_session.xenapi.SR.remove_from_sm_config(
                    self.xapi.srRef, VDI.DB_GC_NO_SPACE)
                xapi_session.xenapi.SR.add_to_sm_config(
                    self.xapi.srRef, VDI.DB_GC_NO_SPACE, msg_id)

            for candidate in self.no_space_candidates.values():
                candidate.setConfig(VDI.DB_GC_NO_SPACE, msg_id)
        elif msg_id is not None:
            # Everything was coalescable, remove the message
            xapi_session.xenapi.message.destroy(msg_id)

    def clear_no_space_msg(self, vdi):
        msg_id = None
        try:
            msg_id = vdi.getConfig(VDI.DB_GC_NO_SPACE)
        except XenAPI.Failure:
            pass

        self.no_space_candidates.pop(vdi.uuid, None)
        if msg_id is not None:
            vdi.delConfig(VDI.DB_GC_NO_SPACE)


    def wait_for_plug(self):
        for _ in range(1, 10):
            time.sleep(2)
            if self.xapi.isPluggedHere():
                return True
        return False

    def gcEnabled(self, refresh=True):
        if refresh:
            self.xapi.srRecord = \
                    self.xapi.session.xenapi.SR.get_record(self.xapi._srRef)
        if self.xapi.srRecord["other_config"].get(VDI.DB_GC) == "false":
            Util.log("GC is disabled for this SR, abort")
            return False
        return True

    def scan(self, force=False):
        """Scan the SR and load VDI info for each VDI. If called repeatedly,
        update VDI objects if they already exist"""
        pass  # abstract

    def scanLocked(self, force=False):
        self.lock()
        try:
            self.scan(force)
        finally:
            self.unlock()

    def getVDI(self, uuid):
        return self.vdis.get(uuid)

    def hasWork(self):
        if len(self.findGarbage()) > 0:
            return True
        if self.findCoalesceable():
            return True
        if self.findLeafCoalesceable():
            return True
        if self.needUpdateBlockInfo():
            return True
        return False

    def findCoalesceable(self):
        """Find a coalesceable VDI. Return a vdi that should be coalesced
        (choosing one among all coalesceable candidates according to some
        criteria) or None if there is no VDI that could be coalesced"""

        candidates = []

        srSwitch = self.xapi.srRecord["other_config"].get(VDI.DB_COALESCE)
        if srSwitch == "false":
            Util.log("Coalesce disabled for this SR")
            return candidates

        # finish any VDI for which a relink journal entry exists first
        journals = self.journaler.getAll(VDI.JRN_RELINK)
        for uuid in journals:
            vdi = self.getVDI(uuid)
            if vdi and vdi not in self._failedCoalesceTargets:
                return vdi

        for vdi in self.vdis.values():
            if vdi.isCoalesceable() and vdi not in self._failedCoalesceTargets:
                candidates.append(vdi)
                Util.log("%s is coalescable" % vdi.uuid)

        self.xapi.update_task_progress("coalescable", len(candidates))

        # pick one in the tallest tree
        treeHeight = dict()
        for c in candidates:
            height = c.getTreeRoot().getTreeHeight()
            if treeHeight.get(height):
                treeHeight[height].append(c)
            else:
                treeHeight[height] = [c]

        freeSpace = self.getFreeSpace()
        heights = list(treeHeight.keys())
        heights.sort(reverse=True)
        for h in heights:
            for c in treeHeight[h]:
                spaceNeeded = c._calcExtraSpaceForCoalescing()
                if spaceNeeded <= freeSpace:
                    Util.log("Coalesce candidate: %s (tree height %d)" % (c, h))
                    self.clear_no_space_msg(c)
                    return c
                else:
                    self.no_space_candidates[c.uuid] = c
                    Util.log("No space to coalesce %s (free space: %d)" % \
                            (c, freeSpace))
        return None

    def getSwitch(self, key):
        return self.xapi.srRecord["other_config"].get(key)

    def forbiddenBySwitch(self, switch, condition, fail_msg):
        srSwitch = self.getSwitch(switch)
        ret = False
        if srSwitch:
            ret = srSwitch == condition

        if ret:
            Util.log(fail_msg)

        return ret

    def leafCoalesceForbidden(self):
        return (self.forbiddenBySwitch(VDI.DB_COALESCE,
                                       "false",
                                       "Coalesce disabled for this SR") or
                self.forbiddenBySwitch(VDI.DB_LEAFCLSC,
                                       VDI.LEAFCLSC_DISABLED,
                                       "Leaf-coalesce disabled for this SR"))

    def findLeafCoalesceable(self):
        """Find leaf-coalesceable VDIs in each VHD tree"""

        candidates = []
        if self.leafCoalesceForbidden():
            return candidates

        self.gatherLeafCoalesceable(candidates)

        self.xapi.update_task_progress("coalescable", len(candidates))

        freeSpace = self.getFreeSpace()
        for candidate in candidates:
            # check the space constraints to see if leaf-coalesce is actually
            # feasible for this candidate
            spaceNeeded = candidate._calcExtraSpaceForSnapshotCoalescing()
            spaceNeededLive = spaceNeeded
            if spaceNeeded > freeSpace:
                spaceNeededLive = candidate._calcExtraSpaceForLeafCoalescing()
                if candidate.canLiveCoalesce(self.getStorageSpeed()):
                    spaceNeeded = spaceNeededLive

            if spaceNeeded <= freeSpace:
                Util.log("Leaf-coalesce candidate: %s" % candidate)
                self.clear_no_space_msg(candidate)
                return candidate
            else:
                Util.log("No space to leaf-coalesce %s (free space: %d)" % \
                        (candidate, freeSpace))
                if spaceNeededLive <= freeSpace:
                    Util.log("...but enough space if skip snap-coalesce")
                    candidate.setConfig(VDI.DB_LEAFCLSC,
                                        VDI.LEAFCLSC_OFFLINE)
                self.no_space_candidates[candidate.uuid] = candidate

        return None

    def gatherLeafCoalesceable(self, candidates):
        for vdi in self.vdis.values():
            if not vdi.isLeafCoalesceable():
                continue
            if vdi in self._failedCoalesceTargets:
                continue
            if vdi.getConfig(vdi.DB_ONBOOT) == vdi.ONBOOT_RESET:
                Util.log("Skipping reset-on-boot %s" % vdi)
                continue
            if vdi.getConfig(vdi.DB_ALLOW_CACHING):
                Util.log("Skipping allow_caching=true %s" % vdi)
                continue
            if vdi.getConfig(vdi.DB_LEAFCLSC) == vdi.LEAFCLSC_DISABLED:
                Util.log("Leaf-coalesce disabled for %s" % vdi)
                continue
            if not (AUTO_ONLINE_LEAF_COALESCE_ENABLED or
                    vdi.getConfig(vdi.DB_LEAFCLSC) == vdi.LEAFCLSC_FORCE):
                continue
            candidates.append(vdi)

    def coalesce(self, vdi, dryRun=False):
        """Coalesce vdi onto parent"""
        Util.log("Coalescing %s -> %s" % (vdi, vdi.parent))
        if dryRun:
            return

        try:
            self._coalesce(vdi)
        except util.SMException as e:
            if isinstance(e, AbortException):
                self.cleanup()
                raise
            else:
                self._failedCoalesceTargets.append(vdi)
                Util.logException("coalesce")
                Util.log("Coalesce failed, skipping")
        self.cleanup()

    def coalesceLeaf(self, vdi, dryRun=False):
        """Leaf-coalesce vdi onto parent"""
        Util.log("Leaf-coalescing %s -> %s" % (vdi, vdi.parent))
        if dryRun:
            return

        try:
            uuid = vdi.uuid
            try:
                # "vdi" object will no longer be valid after this call
                self._coalesceLeaf(vdi)
            finally:
                vdi = self.getVDI(uuid)
                if vdi:
                    vdi.delConfig(vdi.DB_LEAFCLSC)
        except AbortException:
            self.cleanup()
            raise
        except (util.SMException, XenAPI.Failure) as e:
            self._failedCoalesceTargets.append(vdi)
            Util.logException("leaf-coalesce")
            Util.log("Leaf-coalesce failed on %s, skipping" % vdi)
        self.cleanup()

    def garbageCollect(self, dryRun=False):
        vdiList = self.findGarbage()
        Util.log("Found %d VDIs for deletion:" % len(vdiList))
        for vdi in vdiList:
            Util.log("  %s" % vdi)
        if not dryRun:
            self.deleteVDIs(vdiList)
        self.cleanupJournals(dryRun)

    def findGarbage(self):
        vdiList = []
        for vdi in self.vdiTrees:
            vdiList.extend(vdi.getAllPrunable())
        return vdiList

    def deleteVDIs(self, vdiList):
        for vdi in vdiList:
            if IPCFlag(self.uuid).test(FLAG_TYPE_ABORT):
                raise AbortException("Aborting due to signal")
            Util.log("Deleting unlinked VDI %s" % vdi)
            self.deleteVDI(vdi)

    def deleteVDI(self, vdi):
        assert(len(vdi.children) == 0)
        del self.vdis[vdi.uuid]
        if vdi.parent:
            vdi.parent.children.remove(vdi)
        if vdi in self.vdiTrees:
            self.vdiTrees.remove(vdi)
        vdi.delete()

    def forgetVDI(self, vdiUuid):
        self.xapi.forgetVDI(self.uuid, vdiUuid)

    def pauseVDIs(self, vdiList):
        paused = []
        failed = False
        for vdi in vdiList:
            try:
                vdi.pause()
                paused.append(vdi)
            except:
                Util.logException("pauseVDIs")
                failed = True
                break

        if failed:
            self.unpauseVDIs(paused)
            raise util.SMException("Failed to pause VDIs")

    def unpauseVDIs(self, vdiList):
        failed = False
        for vdi in vdiList:
            try:
                vdi.unpause()
            except:
                Util.log("ERROR: Failed to unpause VDI %s" % vdi)
                failed = True
        if failed:
            raise util.SMException("Failed to unpause VDIs")

    def getFreeSpace(self):
        return 0

    def cleanup(self):
        Util.log("In cleanup")
        return

    def __str__(self):
        if self.name:
            ret = "%s ('%s')" % (self.uuid[0:4], self.name)
        else:
            ret = "%s" % self.uuid
        return ret

    def lock(self):
        """Acquire the SR lock. Nested acquire()'s are ok. Check for Abort
        signal to avoid deadlocking (trying to acquire the SR lock while the
        lock is held by a process that is trying to abort us)"""
        if not self._srLock:
            return

        if self._locked == 0:
            abortFlag = IPCFlag(self.uuid)
            for i in range(SR.LOCK_RETRY_ATTEMPTS_LOCK):
                if self._srLock.acquireNoblock():
                    self._locked += 1
                    return
                if abortFlag.test(FLAG_TYPE_ABORT):
                    raise AbortException("Abort requested")
                time.sleep(SR.LOCK_RETRY_INTERVAL)
            raise util.SMException("Unable to acquire the SR lock")

        self._locked += 1

    def unlock(self):
        if not self._srLock:
            return
        assert(self._locked > 0)
        self._locked -= 1
        if self._locked == 0:
            self._srLock.release()

    def needUpdateBlockInfo(self):
        for vdi in self.vdis.values():
            if vdi.scanError or len(vdi.children) == 0:
                continue
            if not vdi.getConfig(vdi.DB_VHD_BLOCKS):
                return True
        return False

    def updateBlockInfo(self):
        for vdi in self.vdis.values():
            if vdi.scanError or len(vdi.children) == 0:
                continue
            if not vdi.getConfig(vdi.DB_VHD_BLOCKS):
                vdi.updateBlockInfo()

    def cleanupCoalesceJournals(self):
        """Remove stale coalesce VDI indicators"""
        entries = self.journaler.getAll(VDI.JRN_COALESCE)
        for uuid, jval in entries.items():
            self.journaler.remove(VDI.JRN_COALESCE, uuid)

    def cleanupJournals(self, dryRun=False):
        """delete journal entries for non-existing VDIs"""
        for t in [LVHDVDI.JRN_ZERO, VDI.JRN_RELINK, SR.JRN_CLONE]:
            entries = self.journaler.getAll(t)
            for uuid, jval in entries.items():
                if self.getVDI(uuid):
                    continue
                if t == SR.JRN_CLONE:
                    baseUuid, clonUuid = jval.split("_")
                    if self.getVDI(baseUuid):
                        continue
                Util.log("  Deleting stale '%s' journal entry for %s "
                        "(%s)" % (t, uuid, jval))
                if not dryRun:
                    self.journaler.remove(t, uuid)

    def cleanupCache(self, maxAge=-1):
        return 0

    def _coalesce(self, vdi):
        if self.journaler.get(vdi.JRN_RELINK, vdi.uuid):
            # this means we had done the actual coalescing already and just
            # need to finish relinking and/or refreshing the children
            Util.log("==> Coalesce apparently already done: skipping")
        else:
            # JRN_COALESCE is used to check which VDI is being coalesced in
            # order to decide whether to abort the coalesce. We remove the
            # journal as soon as the VHD coalesce step is done, because we
            # don't expect the rest of the process to take long
            self.journaler.create(vdi.JRN_COALESCE, vdi.uuid, "1")
            vdi._doCoalesce()
            self.journaler.remove(vdi.JRN_COALESCE, vdi.uuid)

            util.fistpoint.activate("LVHDRT_before_create_relink_journal", self.uuid)

            # we now need to relink the children: lock the SR to prevent ops
            # like SM.clone from manipulating the VDIs we'll be relinking and
            # rescan the SR first in case the children changed since the last
            # scan
            self.journaler.create(vdi.JRN_RELINK, vdi.uuid, "1")

        self.lock()
        try:
            vdi.parent._tagChildrenForRelink()
            self.scan()
            vdi._relinkSkip()
        finally:
            self.unlock()
            # Reload the children to leave things consistent
            vdi.parent._reloadChildren(vdi)

        self.journaler.remove(vdi.JRN_RELINK, vdi.uuid)
        self.deleteVDI(vdi)

    class CoalesceTracker:
        GRACE_ITERATIONS = 2
        MAX_ITERATIONS_NO_PROGRESS = 3
        MAX_ITERATIONS = 10
        MAX_INCREASE_FROM_MINIMUM = 1.2
        HISTORY_STRING = "Iteration: {its} -- Initial size {initSize}" \
                         " --> Final size {finSize}"

        def __init__(self, sr):
            self.itsNoProgress = 0
            self.its = 0
            self.minSize = float("inf")
            self.history = []
            self.reason = ""
            self.startSize = None
            self.finishSize = None
            self.sr = sr
            self.grace_remaining = self.GRACE_ITERATIONS

        def abortCoalesce(self, prevSize, curSize):
            self.its += 1
            self.history.append(self.HISTORY_STRING.format(its=self.its,
                                                           initSize=prevSize,
                                                           finSize=curSize))

            self.finishSize = curSize

            if self.startSize is None:
                self.startSize = prevSize

            if curSize < self.minSize:
                self.minSize = curSize

            if prevSize < self.minSize:
                self.minSize = prevSize

            if self.its == 1:
                # Skip evaluating conditions on first iteration
                return False

            if prevSize < curSize:
                self.itsNoProgress += 1
                Util.log("No progress, attempt:"
                         " {attempt}".format(attempt=self.itsNoProgress))
                util.fistpoint.activate("cleanup_tracker_no_progress", self.sr.uuid)
            else:
                # We made progress
                return False

            if self.its > self.MAX_ITERATIONS:
                max = self.MAX_ITERATIONS
                self.reason = \
                    "Max iterations ({max}) exceeded".format(max=max)
                return True

            if self.itsNoProgress > self.MAX_ITERATIONS_NO_PROGRESS:
                max = self.MAX_ITERATIONS_NO_PROGRESS
                self.reason = \
                    "No progress made for {max} iterations".format(max=max)
                return True

            maxSizeFromMin = self.MAX_INCREASE_FROM_MINIMUM * self.minSize
            if curSize > maxSizeFromMin:
                self.grace_remaining -= 1
                if self.grace_remaining == 0:
                    self.reason = "Unexpected bump in size," \
                        " compared to minimum achieved"

                    return True

            return False

        def printSizes(self):
            Util.log("Starting size was         {size}"
                     .format(size=self.startSize))
            Util.log("Final size was            {size}"
                     .format(size=self.finishSize))
            Util.log("Minimum size achieved was {size}"
                     .format(size=self.minSize))

        def printReasoning(self):
            Util.log("Aborted coalesce")
            for hist in self.history:
                Util.log(hist)
            Util.log(self.reason)
            self.printSizes()

        def printSummary(self):
            if self.its == 0:
                return

            if self.reason:
                Util.log("Aborted coalesce")
                Util.log(self.reason)
            else:
                Util.log("Coalesce summary")

            Util.log(f"Performed {self.its} iterations")
            self.printSizes()


    def _coalesceLeaf(self, vdi):
        """Leaf-coalesce VDI vdi. Return true if we succeed, false if we cannot
        complete due to external changes, namely vdi_delete and vdi_snapshot
        that alter leaf-coalescibility of vdi"""
        tracker = self.CoalesceTracker(self)
        while not vdi.canLiveCoalesce(self.getStorageSpeed()):
            prevSizeVHD = vdi.getSizeVHD()
            if not self._snapshotCoalesce(vdi):
                return False
            if tracker.abortCoalesce(prevSizeVHD, vdi.getSizeVHD()):
                tracker.printReasoning()
                raise util.SMException("VDI {uuid} could not be coalesced"
                                       .format(uuid=vdi.uuid))
        tracker.printSummary()
        return self._liveLeafCoalesce(vdi)

    def calcStorageSpeed(self, startTime, endTime, vhdSize):
        speed = None
        total_time = endTime - startTime
        if total_time > 0:
            speed = float(vhdSize) / float(total_time)
        return speed

    def writeSpeedToFile(self, speed):
        content = []
        speedFile = None
        path = SPEED_LOG_ROOT.format(uuid=self.uuid)
        self.lock()
        try:
            Util.log("Writing to file: {myfile}".format(myfile=path))
            lines = ""
            if not os.path.isfile(path):
                lines = str(speed) + "\n"
            else:
                speedFile = open(path, "r+")
                content = speedFile.readlines()
                content.append(str(speed) + "\n")
                if len(content) > N_RUNNING_AVERAGE:
                    del content[0]
                lines = "".join(content)

            util.atomicFileWrite(path, VAR_RUN, lines)
        finally:
            if speedFile is not None:
                speedFile.close()
            Util.log("Closing file: {myfile}".format(myfile=path))
            self.unlock()

    def recordStorageSpeed(self, startTime, endTime, vhdSize):
        speed = self.calcStorageSpeed(startTime, endTime, vhdSize)
        if speed is None:
            return

        self.writeSpeedToFile(speed)

    def getStorageSpeed(self):
        speedFile = None
        path = SPEED_LOG_ROOT.format(uuid=self.uuid)
        self.lock()
        try:
            speed = None
            if os.path.isfile(path):
                speedFile = open(path)
                content = speedFile.readlines()
                try:
                    content = [float(i) for i in content]
                except ValueError:
                    Util.log("Something bad in the speed log:{log}".
                             format(log=speedFile.readlines()))
                    return speed

                if len(content):
                    speed = sum(content) / float(len(content))
                    if speed <= 0:
                        # Defensive, should be impossible.
                        Util.log("Bad speed: {speed} calculated for SR: {uuid}".
                             format(speed=speed, uuid=self.uuid))
                        speed = None
                else:
                    Util.log("Speed file empty for SR: {uuid}".
                             format(uuid=self.uuid))
            else:
                Util.log("Speed log missing for SR: {uuid}".
                         format(uuid=self.uuid))
            return speed
        finally:
            if not (speedFile is None):
                speedFile.close()
            self.unlock()

    def _snapshotCoalesce(self, vdi):
        # Note that because we are not holding any locks here, concurrent SM
        # operations may change this tree under our feet. In particular, vdi
        # can be deleted, or it can be snapshotted.
        assert(AUTO_ONLINE_LEAF_COALESCE_ENABLED)
        Util.log("Single-snapshotting %s" % vdi)
        util.fistpoint.activate("LVHDRT_coaleaf_delay_1", self.uuid)
        try:
            ret = self.xapi.singleSnapshotVDI(vdi)
            Util.log("Single-snapshot returned: %s" % ret)
        except XenAPI.Failure as e:
            if util.isInvalidVDI(e):
                Util.log("The VDI appears to have been concurrently deleted")
                return False
            raise
        self.scanLocked()
        tempSnap = vdi.parent
        if not tempSnap.isCoalesceable():
            Util.log("The VDI appears to have been concurrently snapshotted")
            return False
        Util.log("Coalescing parent %s" % tempSnap)
        util.fistpoint.activate("LVHDRT_coaleaf_delay_2", self.uuid)
        vhdSize = vdi.getSizeVHD()
        self._coalesce(tempSnap)
        if not vdi.isLeafCoalesceable():
            Util.log("The VDI tree appears to have been altered since")
            return False
        return True

    def _liveLeafCoalesce(self, vdi):
        util.fistpoint.activate("LVHDRT_coaleaf_delay_3", self.uuid)
        self.lock()
        try:
            self.scan()
            if not self.getVDI(vdi.uuid):
                Util.log("The VDI appears to have been deleted meanwhile")
                return False
            if not vdi.isLeafCoalesceable():
                Util.log("The VDI is no longer leaf-coalesceable")
                return False

            uuid = vdi.uuid
            vdi.pause(failfast=True)
            try:
                try:
                    # "vdi" object will no longer be valid after this call
                    self._doCoalesceLeaf(vdi)
                except:
                    Util.logException("_doCoalesceLeaf")
                    self._handleInterruptedCoalesceLeaf()
                    raise
            finally:
                vdi = self.getVDI(uuid)
                if vdi:
                    vdi.ensureUnpaused()
                vdiOld = self.getVDI(self.TMP_RENAME_PREFIX + uuid)
                if vdiOld:
                    util.fistpoint.activate("LVHDRT_coaleaf_before_delete", self.uuid)
                    self.deleteVDI(vdiOld)
                    util.fistpoint.activate("LVHDRT_coaleaf_after_delete", self.uuid)
        finally:
            self.cleanup()
            self.unlock()
            self.logFilter.logState()
        return True

    def _doCoalesceLeaf(self, vdi):
        """Actual coalescing of a leaf VDI onto parent. Must be called in an
        offline/atomic context"""
        self.journaler.create(VDI.JRN_LEAF, vdi.uuid, vdi.parent.uuid)
        self._prepareCoalesceLeaf(vdi)
        vdi.parent._setHidden(False)
        vdi.parent._increaseSizeVirt(vdi.sizeVirt, False)
        vdi.validate(True)
        vdi.parent.validate(True)
        util.fistpoint.activate("LVHDRT_coaleaf_before_coalesce", self.uuid)
        timeout = vdi.LIVE_LEAF_COALESCE_TIMEOUT
        if vdi.getConfig(vdi.DB_LEAFCLSC) == vdi.LEAFCLSC_FORCE:
            Util.log("Leaf-coalesce forced, will not use timeout")
            timeout = 0
        vdi._coalesceVHD(timeout)
        util.fistpoint.activate("LVHDRT_coaleaf_after_coalesce", self.uuid)
        vdi.parent.validate(True)
        #vdi._verifyContents(timeout / 2)

        # rename
        vdiUuid = vdi.uuid
        oldName = vdi.fileName
        origParentUuid = vdi.parent.uuid
        vdi.rename(self.TMP_RENAME_PREFIX + vdiUuid)
        util.fistpoint.activate("LVHDRT_coaleaf_one_renamed", self.uuid)
        vdi.parent.rename(vdiUuid)
        util.fistpoint.activate("LVHDRT_coaleaf_both_renamed", self.uuid)
        self._updateSlavesOnRename(vdi.parent, oldName, origParentUuid)

        # Note that "vdi.parent" is now the single remaining leaf and "vdi" is
        # garbage

        # update the VDI record
        vdi.parent.delConfig(VDI.DB_VHD_PARENT)
        if vdi.parent.raw:
            vdi.parent.setConfig(VDI.DB_VDI_TYPE, vhdutil.VDI_TYPE_RAW)
        vdi.parent.delConfig(VDI.DB_VHD_BLOCKS)
        util.fistpoint.activate("LVHDRT_coaleaf_after_vdirec", self.uuid)

        self._updateNode(vdi)

        # delete the obsolete leaf & inflate the parent (in that order, to
        # minimize free space requirements)
        parent = vdi.parent
        vdi._setHidden(True)
        vdi.parent.children = []
        vdi.parent = None

        extraSpace = self._calcExtraSpaceNeeded(vdi, parent)
        freeSpace = self.getFreeSpace()
        if freeSpace < extraSpace:
            # don't delete unless we need the space: deletion is time-consuming
            # because it requires contacting the slaves, and we're paused here
            util.fistpoint.activate("LVHDRT_coaleaf_before_delete", self.uuid)
            self.deleteVDI(vdi)
            util.fistpoint.activate("LVHDRT_coaleaf_after_delete", self.uuid)

        util.fistpoint.activate("LVHDRT_coaleaf_before_remove_j", self.uuid)
        self.journaler.remove(VDI.JRN_LEAF, vdiUuid)

        self.forgetVDI(origParentUuid)
        self._finishCoalesceLeaf(parent)
        self._updateSlavesOnResize(parent)

    def _calcExtraSpaceNeeded(self, child, parent):
        assert(not parent.raw)  # raw parents not supported
        extra = child.getSizeVHD() - parent.getSizeVHD()
        if extra < 0:
            extra = 0
        return extra

    def _prepareCoalesceLeaf(self, vdi):
        pass

    def _updateNode(self, vdi):
        pass

    def _finishCoalesceLeaf(self, parent):
        pass

    def _updateSlavesOnUndoLeafCoalesce(self, parent, child):
        pass

    def _updateSlavesOnRename(self, vdi, oldName, origParentUuid):
        pass

    def _updateSlavesOnResize(self, vdi):
        pass

    def _removeStaleVDIs(self, uuidsPresent):
        for uuid in list(self.vdis.keys()):
            if not uuid in uuidsPresent:
                Util.log("VDI %s disappeared since last scan" % \
                        self.vdis[uuid])
                del self.vdis[uuid]

    def _handleInterruptedCoalesceLeaf(self):
        """An interrupted leaf-coalesce operation may leave the VHD tree in an
        inconsistent state. If the old-leaf VDI is still present, we revert the
        operation (in case the original error is persistent); otherwise we must
        finish the operation"""
        # abstract
        pass

    def _buildTree(self, force):
        self.vdiTrees = []
        for vdi in self.vdis.values():
            if vdi.parentUuid:
                parent = self.getVDI(vdi.parentUuid)
                if not parent:
                    if vdi.uuid.startswith(self.TMP_RENAME_PREFIX):
                        self.vdiTrees.append(vdi)
                        continue
                    if force:
                        Util.log("ERROR: Parent VDI %s not found! (for %s)" % \
                                (vdi.parentUuid, vdi.uuid))
                        self.vdiTrees.append(vdi)
                        continue
                    else:
                        raise util.SMException("Parent VDI %s of %s not " \
                                "found" % (vdi.parentUuid, vdi.uuid))
                vdi.parent = parent
                parent.children.append(vdi)
            else:
                self.vdiTrees.append(vdi)


class FileSR(SR):
    TYPE = SR.TYPE_FILE
    CACHE_FILE_EXT = ".vhdcache"
    # cache cleanup actions
    CACHE_ACTION_KEEP = 0
    CACHE_ACTION_REMOVE = 1
    CACHE_ACTION_REMOVE_IF_INACTIVE = 2

    def __init__(self, uuid, xapi, createLock, force):
        SR.__init__(self, uuid, xapi, createLock, force)
        self.path = "/run/sr-mount/%s" % self.uuid
        self.journaler = fjournaler.Journaler(self.path)

    def scan(self, force=False):
        if not util.pathexists(self.path):
            raise util.SMException("directory %s not found!" % self.uuid)
        vhds = self._scan(force)
        for uuid, vhdInfo in vhds.items():
            vdi = self.getVDI(uuid)
            if not vdi:
                self.logFilter.logNewVDI(uuid)
                vdi = FileVDI(self, uuid, False)
                self.vdis[uuid] = vdi
            vdi.load(vhdInfo)
        uuidsPresent = list(vhds.keys())
        rawList = [x for x in os.listdir(self.path) if x.endswith(vhdutil.FILE_EXTN_RAW)]
        for rawName in rawList:
            uuid = FileVDI.extractUuid(rawName)
            uuidsPresent.append(uuid)
            vdi = self.getVDI(uuid)
            if not vdi:
                self.logFilter.logNewVDI(uuid)
                vdi = FileVDI(self, uuid, True)
                self.vdis[uuid] = vdi
        self._removeStaleVDIs(uuidsPresent)
        self._buildTree(force)
        self.logFilter.logState()
        self._handleInterruptedCoalesceLeaf()

    def getFreeSpace(self):
        return util.get_fs_size(self.path) - util.get_fs_utilisation(self.path)

    def deleteVDIs(self, vdiList):
        rootDeleted = False
        for vdi in vdiList:
            if not vdi.parent:
                rootDeleted = True
                break
        SR.deleteVDIs(self, vdiList)
        if self.xapi.srRecord["type"] == "nfs" and rootDeleted:
            self.xapi.markCacheSRsDirty()

    def cleanupCache(self, maxAge=-1):
        """Clean up IntelliCache cache files. Caches for leaf nodes are
        removed when the leaf node no longer exists or its allow-caching
        attribute is not set. Caches for parent nodes are removed when the
        parent node no longer exists or it hasn't been used in more than
        <maxAge> hours.
        Return number of caches removed.
        """
        numRemoved = 0
        cacheFiles = [x for x in os.listdir(self.path) if self._isCacheFileName(x)]
        Util.log("Found %d cache files" % len(cacheFiles))
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=maxAge)
        for cacheFile in cacheFiles:
            uuid = cacheFile[:-len(self.CACHE_FILE_EXT)]
            action = self.CACHE_ACTION_KEEP
            rec = self.xapi.getRecordVDI(uuid)
            if not rec:
                Util.log("Cache %s: VDI doesn't exist" % uuid)
                action = self.CACHE_ACTION_REMOVE
            elif rec["managed"] and not rec["allow_caching"]:
                Util.log("Cache %s: caching disabled" % uuid)
                action = self.CACHE_ACTION_REMOVE
            elif not rec["managed"] and maxAge >= 0:
                lastAccess = datetime.datetime.fromtimestamp( \
                        os.path.getatime(os.path.join(self.path, cacheFile)))
                if lastAccess < cutoff:
                    Util.log("Cache %s: older than %d hrs" % (uuid, maxAge))
                    action = self.CACHE_ACTION_REMOVE_IF_INACTIVE

            if action == self.CACHE_ACTION_KEEP:
                Util.log("Keeping cache %s" % uuid)
                continue

            lockId = uuid
            parentUuid = None
            if rec and rec["managed"]:
                parentUuid = rec["sm_config"].get("vhd-parent")
            if parentUuid:
                lockId = parentUuid

            cacheLock = lock.Lock(blktap2.VDI.LOCK_CACHE_SETUP, lockId)
            cacheLock.acquire()
            try:
                if self._cleanupCache(uuid, action):
                    numRemoved += 1
            finally:
                cacheLock.release()
        return numRemoved

    def _cleanupCache(self, uuid, action):
        assert(action != self.CACHE_ACTION_KEEP)
        rec = self.xapi.getRecordVDI(uuid)
        if rec and rec["allow_caching"]:
            Util.log("Cache %s appears to have become valid" % uuid)
            return False

        fullPath = os.path.join(self.path, uuid + self.CACHE_FILE_EXT)
        tapdisk = blktap2.Tapdisk.find_by_path(fullPath)
        if tapdisk:
            if action == self.CACHE_ACTION_REMOVE_IF_INACTIVE:
                Util.log("Cache %s still in use" % uuid)
                return False
            Util.log("Shutting down tapdisk for %s" % fullPath)
            tapdisk.shutdown()

        Util.log("Deleting file %s" % fullPath)
        os.unlink(fullPath)
        return True

    def _isCacheFileName(self, name):
        return (len(name) == Util.UUID_LEN + len(self.CACHE_FILE_EXT)) and \
                name.endswith(self.CACHE_FILE_EXT)

    def _scan(self, force):
        for i in range(SR.SCAN_RETRY_ATTEMPTS):
            error = False
            pattern = os.path.join(self.path, "*%s" % vhdutil.FILE_EXTN_VHD)
            vhds = vhdutil.getAllVHDs(pattern, FileVDI.extractUuid)
            for uuid, vhdInfo in vhds.items():
                if vhdInfo.error:
                    error = True
                    break
            if not error:
                return vhds
            Util.log("Scan error on attempt %d" % i)
        if force:
            return vhds
        raise util.SMException("Scan error")

    def deleteVDI(self, vdi):
        self._checkSlaves(vdi)
        SR.deleteVDI(self, vdi)

    def _checkSlaves(self, vdi):
        onlineHosts = self.xapi.getOnlineHosts()
        abortFlag = IPCFlag(self.uuid)
        for pbdRecord in self.xapi.getAttachedPBDs():
            hostRef = pbdRecord["host"]
            if hostRef == self.xapi._hostRef:
                continue
            if abortFlag.test(FLAG_TYPE_ABORT):
                raise AbortException("Aborting due to signal")
            try:
                self._checkSlave(hostRef, vdi)
            except util.CommandException:
                if hostRef in onlineHosts:
                    raise

    def _checkSlave(self, hostRef, vdi):
        call = (hostRef, "nfs-on-slave", "check", {'path': vdi.path})
        Util.log("Checking with slave: %s" % repr(call))
        _host = self.xapi.session.xenapi.host
        text = _host.call_plugin( * call)

    def _handleInterruptedCoalesceLeaf(self):
        entries = self.journaler.getAll(VDI.JRN_LEAF)
        for uuid, parentUuid in entries.items():
            fileList = os.listdir(self.path)
            childName = uuid + vhdutil.FILE_EXTN_VHD
            tmpChildName = self.TMP_RENAME_PREFIX + uuid + vhdutil.FILE_EXTN_VHD
            parentName1 = parentUuid + vhdutil.FILE_EXTN_VHD
            parentName2 = parentUuid + vhdutil.FILE_EXTN_RAW
            parentPresent = (parentName1 in fileList or parentName2 in fileList)
            if parentPresent or tmpChildName in fileList:
                self._undoInterruptedCoalesceLeaf(uuid, parentUuid)
            else:
                self._finishInterruptedCoalesceLeaf(uuid, parentUuid)
            self.journaler.remove(VDI.JRN_LEAF, uuid)
            vdi = self.getVDI(uuid)
            if vdi:
                vdi.ensureUnpaused()

    def _undoInterruptedCoalesceLeaf(self, childUuid, parentUuid):
        Util.log("*** UNDO LEAF-COALESCE")
        parent = self.getVDI(parentUuid)
        if not parent:
            parent = self.getVDI(childUuid)
            if not parent:
                raise util.SMException("Neither %s nor %s found" % \
                        (parentUuid, childUuid))
            Util.log("Renaming parent back: %s -> %s" % (childUuid, parentUuid))
            parent.rename(parentUuid)
        util.fistpoint.activate("LVHDRT_coaleaf_undo_after_rename", self.uuid)

        child = self.getVDI(childUuid)
        if not child:
            child = self.getVDI(self.TMP_RENAME_PREFIX + childUuid)
            if not child:
                raise util.SMException("Neither %s nor %s found" % \
                        (childUuid, self.TMP_RENAME_PREFIX + childUuid))
            Util.log("Renaming child back to %s" % childUuid)
            child.rename(childUuid)
            Util.log("Updating the VDI record")
            child.setConfig(VDI.DB_VHD_PARENT, parentUuid)
            child.setConfig(VDI.DB_VDI_TYPE, vhdutil.VDI_TYPE_VHD)
            util.fistpoint.activate("LVHDRT_coaleaf_undo_after_rename2", self.uuid)

        if child.hidden:
            child._setHidden(False)
        if not parent.hidden:
            parent._setHidden(True)
        self._updateSlavesOnUndoLeafCoalesce(parent, child)
        util.fistpoint.activate("LVHDRT_coaleaf_undo_end", self.uuid)
        Util.log("*** leaf-coalesce undo successful")
        if util.fistpoint.is_active("LVHDRT_coaleaf_stop_after_recovery"):
            child.setConfig(VDI.DB_LEAFCLSC, VDI.LEAFCLSC_DISABLED)

    def _finishInterruptedCoalesceLeaf(self, childUuid, parentUuid):
        Util.log("*** FINISH LEAF-COALESCE")
        vdi = self.getVDI(childUuid)
        if not vdi:
            raise util.SMException("VDI %s not found" % childUuid)
        try:
            self.forgetVDI(parentUuid)
        except XenAPI.Failure:
            pass
        self._updateSlavesOnResize(vdi)
        util.fistpoint.activate("LVHDRT_coaleaf_finish_end", self.uuid)
        Util.log("*** finished leaf-coalesce successfully")


class LVHDSR(SR):
    TYPE = SR.TYPE_LVHD
    SUBTYPES = ["lvhdoiscsi", "lvhdohba"]

    def __init__(self, uuid, xapi, createLock, force):
        SR.__init__(self, uuid, xapi, createLock, force)
        self.vgName = "%s%s" % (lvhdutil.VG_PREFIX, self.uuid)
        self.path = os.path.join(lvhdutil.VG_LOCATION, self.vgName)
        self.lvmCache = lvmcache.LVMCache(self.vgName)
        self.lvActivator = LVActivator(self.uuid, self.lvmCache)
        self.journaler = journaler.Journaler(self.lvmCache)

    def deleteVDI(self, vdi):
        if self.lvActivator.get(vdi.uuid, False):
            self.lvActivator.deactivate(vdi.uuid, False)
        self._checkSlaves(vdi)
        SR.deleteVDI(self, vdi)

    def forgetVDI(self, vdiUuid):
        SR.forgetVDI(self, vdiUuid)
        mdpath = os.path.join(self.path, lvutil.MDVOLUME_NAME)
        LVMMetadataHandler(mdpath).deleteVdiFromMetadata(vdiUuid)

    def getFreeSpace(self):
        stats = lvutil._getVGstats(self.vgName)
        return stats['physical_size'] - stats['physical_utilisation']

    def cleanup(self):
        if not self.lvActivator.deactivateAll():
            Util.log("ERROR deactivating LVs while cleaning up")

    def needUpdateBlockInfo(self):
        for vdi in self.vdis.values():
            if vdi.scanError or vdi.raw or len(vdi.children) == 0:
                continue
            if not vdi.getConfig(vdi.DB_VHD_BLOCKS):
                return True
        return False

    def updateBlockInfo(self):
        numUpdated = 0
        for vdi in self.vdis.values():
            if vdi.scanError or vdi.raw or len(vdi.children) == 0:
                continue
            if not vdi.getConfig(vdi.DB_VHD_BLOCKS):
                vdi.updateBlockInfo()
                numUpdated += 1
        if numUpdated:
            # deactivate the LVs back sooner rather than later. If we don't
            # now, by the time this thread gets to deactivations, another one
            # might have leaf-coalesced a node and deleted it, making the child
            # inherit the refcount value and preventing the correct decrement
            self.cleanup()

    def scan(self, force=False):
        vdis = self._scan(force)
        for uuid, vdiInfo in vdis.items():
            vdi = self.getVDI(uuid)
            if not vdi:
                self.logFilter.logNewVDI(uuid)
                vdi = LVHDVDI(self, uuid,
                        vdiInfo.vdiType == vhdutil.VDI_TYPE_RAW)
                self.vdis[uuid] = vdi
            vdi.load(vdiInfo)
        self._removeStaleVDIs(vdis.keys())
        self._buildTree(force)
        self.logFilter.logState()
        self._handleInterruptedCoalesceLeaf()

    def _scan(self, force):
        for i in range(SR.SCAN_RETRY_ATTEMPTS):
            error = False
            self.lvmCache.refresh()
            vdis = lvhdutil.getVDIInfo(self.lvmCache)
            for uuid, vdiInfo in vdis.items():
                if vdiInfo.scanError:
                    error = True
                    break
            if not error:
                return vdis
            Util.log("Scan error, retrying (%d)" % i)
        if force:
            return vdis
        raise util.SMException("Scan error")

    def _removeStaleVDIs(self, uuidsPresent):
        for uuid in list(self.vdis.keys()):
            if not uuid in uuidsPresent:
                Util.log("VDI %s disappeared since last scan" % \
                        self.vdis[uuid])
                del self.vdis[uuid]
                if self.lvActivator.get(uuid, False):
                    self.lvActivator.remove(uuid, False)

    def _liveLeafCoalesce(self, vdi):
        """If the parent is raw and the child was resized (virt. size), then
        we'll need to resize the parent, which can take a while due to zeroing
        out of the extended portion of the LV. Do it before pausing the child
        to avoid a protracted downtime"""
        if vdi.parent.raw and vdi.sizeVirt > vdi.parent.sizeVirt:
            self.lvmCache.setReadonly(vdi.parent.fileName, False)
            vdi.parent._increaseSizeVirt(vdi.sizeVirt)

        return SR._liveLeafCoalesce(self, vdi)

    def _prepareCoalesceLeaf(self, vdi):
        vdi._activateChain()
        self.lvmCache.setReadonly(vdi.parent.fileName, False)
        vdi.deflate()
        vdi.inflateParentForCoalesce()

    def _updateNode(self, vdi):
        # fix the refcounts: the remaining node should inherit the binary
        # refcount from the leaf (because if it was online, it should remain
        # refcounted as such), but the normal refcount from the parent (because
        # this node is really the parent node) - minus 1 if it is online (since
        # non-leaf nodes increment their normal counts when they are online and
        # we are now a leaf, storing that 1 in the binary refcount).
        ns = lvhdutil.NS_PREFIX_LVM + self.uuid
        cCnt, cBcnt = RefCounter.check(vdi.uuid, ns)
        pCnt, pBcnt = RefCounter.check(vdi.parent.uuid, ns)
        pCnt = pCnt - cBcnt
        assert(pCnt >= 0)
        RefCounter.set(vdi.parent.uuid, pCnt, cBcnt, ns)

    def _finishCoalesceLeaf(self, parent):
        if not parent.isSnapshot() or parent.isAttachedRW():
            parent.inflateFully()
        else:
            parent.deflate()

    def _calcExtraSpaceNeeded(self, child, parent):
        return lvhdutil.calcSizeVHDLV(parent.sizeVirt) - parent.sizeLV

    def _handleInterruptedCoalesceLeaf(self):
        entries = self.journaler.getAll(VDI.JRN_LEAF)
        for uuid, parentUuid in entries.items():
            childLV = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_VHD] + uuid
            tmpChildLV = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_VHD] + \
                    self.TMP_RENAME_PREFIX + uuid
            parentLV1 = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_VHD] + parentUuid
            parentLV2 = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_RAW] + parentUuid
            parentPresent = (self.lvmCache.checkLV(parentLV1) or \
                    self.lvmCache.checkLV(parentLV2))
            if parentPresent or self.lvmCache.checkLV(tmpChildLV):
                self._undoInterruptedCoalesceLeaf(uuid, parentUuid)
            else:
                self._finishInterruptedCoalesceLeaf(uuid, parentUuid)
            self.journaler.remove(VDI.JRN_LEAF, uuid)
            vdi = self.getVDI(uuid)
            if vdi:
                vdi.ensureUnpaused()

    def _undoInterruptedCoalesceLeaf(self, childUuid, parentUuid):
        Util.log("*** UNDO LEAF-COALESCE")
        parent = self.getVDI(parentUuid)
        if not parent:
            parent = self.getVDI(childUuid)
            if not parent:
                raise util.SMException("Neither %s nor %s found" % \
                        (parentUuid, childUuid))
            Util.log("Renaming parent back: %s -> %s" % (childUuid, parentUuid))
            parent.rename(parentUuid)
        util.fistpoint.activate("LVHDRT_coaleaf_undo_after_rename", self.uuid)

        child = self.getVDI(childUuid)
        if not child:
            child = self.getVDI(self.TMP_RENAME_PREFIX + childUuid)
            if not child:
                raise util.SMException("Neither %s nor %s found" % \
                        (childUuid, self.TMP_RENAME_PREFIX + childUuid))
            Util.log("Renaming child back to %s" % childUuid)
            child.rename(childUuid)
            Util.log("Updating the VDI record")
            child.setConfig(VDI.DB_VHD_PARENT, parentUuid)
            child.setConfig(VDI.DB_VDI_TYPE, vhdutil.VDI_TYPE_VHD)
            util.fistpoint.activate("LVHDRT_coaleaf_undo_after_rename2", self.uuid)

            # refcount (best effort - assume that it had succeeded if the
            # second rename succeeded; if not, this adjustment will be wrong,
            # leading to a non-deactivation of the LV)
            ns = lvhdutil.NS_PREFIX_LVM + self.uuid
            cCnt, cBcnt = RefCounter.check(child.uuid, ns)
            pCnt, pBcnt = RefCounter.check(parent.uuid, ns)
            pCnt = pCnt + cBcnt
            RefCounter.set(parent.uuid, pCnt, 0, ns)
            util.fistpoint.activate("LVHDRT_coaleaf_undo_after_refcount", self.uuid)

        parent.deflate()
        child.inflateFully()
        util.fistpoint.activate("LVHDRT_coaleaf_undo_after_deflate", self.uuid)
        if child.hidden:
            child._setHidden(False)
        if not parent.hidden:
            parent._setHidden(True)
        if not parent.lvReadonly:
            self.lvmCache.setReadonly(parent.fileName, True)
        self._updateSlavesOnUndoLeafCoalesce(parent, child)
        util.fistpoint.activate("LVHDRT_coaleaf_undo_end", self.uuid)
        Util.log("*** leaf-coalesce undo successful")
        if util.fistpoint.is_active("LVHDRT_coaleaf_stop_after_recovery"):
            child.setConfig(VDI.DB_LEAFCLSC, VDI.LEAFCLSC_DISABLED)

    def _finishInterruptedCoalesceLeaf(self, childUuid, parentUuid):
        Util.log("*** FINISH LEAF-COALESCE")
        vdi = self.getVDI(childUuid)
        if not vdi:
            raise util.SMException("VDI %s not found" % childUuid)
        vdi.inflateFully()
        util.fistpoint.activate("LVHDRT_coaleaf_finish_after_inflate", self.uuid)
        try:
            self.forgetVDI(parentUuid)
        except XenAPI.Failure:
            pass
        self._updateSlavesOnResize(vdi)
        util.fistpoint.activate("LVHDRT_coaleaf_finish_end", self.uuid)
        Util.log("*** finished leaf-coalesce successfully")

    def _checkSlaves(self, vdi):
        """Confirm with all slaves in the pool that 'vdi' is not in use. We
        try to check all slaves, including those that the Agent believes are
        offline, but ignore failures for offline hosts. This is to avoid cases
        where the Agent thinks a host is offline but the host is up."""
        args = {"vgName": self.vgName,
                "action1": "deactivateNoRefcount",
                "lvName1": vdi.fileName,
                "action2": "cleanupLockAndRefcount",
                "uuid2": vdi.uuid,
                "ns2": lvhdutil.NS_PREFIX_LVM + self.uuid}
        onlineHosts = self.xapi.getOnlineHosts()
        abortFlag = IPCFlag(self.uuid)
        for pbdRecord in self.xapi.getAttachedPBDs():
            hostRef = pbdRecord["host"]
            if hostRef == self.xapi._hostRef:
                continue
            if abortFlag.test(FLAG_TYPE_ABORT):
                raise AbortException("Aborting due to signal")
            Util.log("Checking with slave %s (path %s)" % (
                self.xapi.getRecordHost(hostRef)['hostname'], vdi.path))
            try:
                self.xapi.ensureInactive(hostRef, args)
            except XenAPI.Failure:
                if hostRef in onlineHosts:
                    raise

    def _updateSlavesOnUndoLeafCoalesce(self, parent, child):
        slaves = util.get_slaves_attached_on(self.xapi.session, [child.uuid])
        if not slaves:
            Util.log("Update-on-leaf-undo: VDI %s not attached on any slave" % \
                    child)
            return

        tmpName = lvhdutil.LV_PREFIX[vhdutil.VDI_TYPE_VHD] + \
                self.TMP_RENAME_PREFIX + child.uuid
        args = {"vgName": self.vgName,
                "action1": "deactivateNoRefcount",
                "lvName1": tmpName,
                "action2": "deactivateNoRefcount",
                "lvName2": child.fileName,
                "action3": "refresh",
                "lvName3": child.fileName,
                "action4": "refresh",
                "lvName4": parent.fileName}
        for slave in slaves:
            Util.log("Updating %s, %s, %s on slave %s" % \
                    (tmpName, child.fileName, parent.fileName,
                     self.xapi.getRecordHost(slave)['hostname']))
            text = self.xapi.session.xenapi.host.call_plugin( \
                    slave, self.xapi.PLUGIN_ON_SLAVE, "multi", args)
            Util.log("call-plugin returned: '%s'" % text)

    def _updateSlavesOnRename(self, vdi, oldNameLV, origParentUuid):
        slaves = util.get_slaves_attached_on(self.xapi.session, [vdi.uuid])
        if not slaves:
            Util.log("Update-on-rename: VDI %s not attached on any slave" % vdi)
            return

        args = {"vgName": self.vgName,
                "action1": "deactivateNoRefcount",
                "lvName1": oldNameLV,
                "action2": "refresh",
                "lvName2": vdi.fileName,
                "action3": "cleanupLockAndRefcount",
                "uuid3": origParentUuid,
                "ns3": lvhdutil.NS_PREFIX_LVM + self.uuid}
        for slave in slaves:
            Util.log("Updating %s to %s on slave %s" % \
                    (oldNameLV, vdi.fileName,
                     self.xapi.getRecordHost(slave)['hostname']))
            text = self.xapi.session.xenapi.host.call_plugin( \
                    slave, self.xapi.PLUGIN_ON_SLAVE, "multi", args)
            Util.log("call-plugin returned: '%s'" % text)

    def _updateSlavesOnResize(self, vdi):
        uuids = [x.uuid for x in vdi.getAllLeaves()]
        slaves = util.get_slaves_attached_on(self.xapi.session, uuids)
        if not slaves:
            util.SMlog("Update-on-resize: %s not attached on any slave" % vdi)
            return
        lvhdutil.lvRefreshOnSlaves(self.xapi.session, self.uuid, self.vgName,
                vdi.fileName, vdi.uuid, slaves)


################################################################################
#
#  Helpers
#
def daemonize():
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        Util.log("New PID [%d]" % pid)
        return False
    os.chdir("/")
    os.setsid()
    pid = os.fork()
    if pid:
        Util.log("Will finish as PID [%d]" % pid)
        os._exit(0)
    for fd in [0, 1, 2]:
        try:
            os.close(fd)
        except OSError:
            pass
    # we need to fill those special fd numbers or pread won't work
    sys.stdin = open("/dev/null", 'r')
    sys.stderr = open("/dev/null", 'w')
    sys.stdout = open("/dev/null", 'w')
    # As we're a new process we need to clear the lock objects
    lock.Lock.clearAll()
    return True


def normalizeType(type):
    if type in LVHDSR.SUBTYPES:
        type = SR.TYPE_LVHD
    if type in ["lvm", "lvmoiscsi", "lvmohba"]:
        # temporary while LVHD is symlinked as LVM
        type = SR.TYPE_LVHD
    if type in ["ext", "nfs", "ocfsoiscsi", "ocfsohba", "smb"]:
        type = SR.TYPE_FILE
    if not type in SR.TYPES:
        raise util.SMException("Unsupported SR type: %s" % type)
    return type

GCPAUSE_DEFAULT_SLEEP = 5 * 60


def _gc_init_file(sr_uuid):
    return os.path.join(NON_PERSISTENT_DIR, str(sr_uuid), 'gc_init')


def _create_init_file(sr_uuid):
    util.makedirs(os.path.join(NON_PERSISTENT_DIR, str(sr_uuid)))
    with open(os.path.join(
            NON_PERSISTENT_DIR, str(sr_uuid), 'gc_init'), 'w+') as f:
        f.write('1')


def _gcLoopPause(sr, dryRun=False, immediate=False):
    if immediate:
        return

    # Check to see if the GCPAUSE_FISTPOINT is present. If so the fist
    # point will just return. Otherwise, fall back on an abortable sleep.

    if util.fistpoint.is_active(util.GCPAUSE_FISTPOINT):

        util.fistpoint.activate_custom_fn(util.GCPAUSE_FISTPOINT,
                                          lambda *args: None)
    elif os.path.exists(_gc_init_file(sr.uuid)):
        def abortTest():
            return IPCFlag(sr.uuid).test(FLAG_TYPE_ABORT)

        # If time.sleep hangs we are in deep trouble, however for
        # completeness we set the timeout of the abort thread to
        # 110% of GCPAUSE_DEFAULT_SLEEP.
        Util.log("GC active, about to go quiet")
        Util.runAbortable(lambda: time.sleep(GCPAUSE_DEFAULT_SLEEP),
                          None, sr.uuid, abortTest, VDI.POLL_INTERVAL,
                          GCPAUSE_DEFAULT_SLEEP * 1.1)
        Util.log("GC active, quiet period ended")


def _gcLoop(sr, dryRun=False, immediate=False):
    if not lockGCActive.acquireNoblock():
        Util.log("Another GC instance already active, exiting")
        return

    # Check we're still attached after acquiring locks
    if not sr.xapi.isPluggedHere():
        Util.log("SR no longer attached, exiting")
        return

    # Clean up Intellicache files
    sr.cleanupCache()

    # Track how many we do
    coalesced = 0
    task_status = "success"
    try:
        # Check if any work needs to be done
        if not sr.xapi.isPluggedHere():
            Util.log("SR no longer attached, exiting")
            return
        sr.scanLocked()
        if not sr.hasWork():
            Util.log("No work, exiting")
            return
        sr.xapi.create_task(
            "Garbage Collection",
            "Garbage collection for SR %s" % sr.uuid)
        _gcLoopPause(sr, dryRun, immediate=immediate)
        while True:
            if SIGTERM:
                Util.log("Term requested")
                return

            if not sr.xapi.isPluggedHere():
                Util.log("SR no longer attached, exiting")
                break
            sr.scanLocked()
            if not sr.hasWork():
                Util.log("No work, exiting")
                break

            if not lockGCRunning.acquireNoblock():
                Util.log("Unable to acquire GC running lock.")
                return
            try:
                if not sr.gcEnabled():
                    break

                sr.xapi.update_task_progress("done", coalesced)

                sr.cleanupCoalesceJournals()
                # Create the init file here in case startup is waiting on it
                _create_init_file(sr.uuid)
                sr.scanLocked()
                sr.updateBlockInfo()

                howmany = len(sr.findGarbage())
                if howmany > 0:
                    Util.log("Found %d orphaned vdis" % howmany)
                    sr.lock()
                    try:
                        sr.garbageCollect(dryRun)
                    finally:
                        sr.unlock()
                    sr.xapi.srUpdate()

                candidate = sr.findCoalesceable()
                if candidate:
                    util.fistpoint.activate(
                        "LVHDRT_finding_a_suitable_pair", sr.uuid)
                    sr.coalesce(candidate, dryRun)
                    sr.xapi.srUpdate()
                    coalesced += 1
                    continue

                candidate = sr.findLeafCoalesceable()
                if candidate:
                    sr.coalesceLeaf(candidate, dryRun)
                    sr.xapi.srUpdate()
                    coalesced += 1
                    continue

            finally:
                lockGCRunning.release()
    except:
        task_status = "failure"
        raise
    finally:
        sr.xapi.set_task_status(task_status)
        Util.log("GC process exiting, no work left")
        _create_init_file(sr.uuid)
        lockGCActive.release()


def _gc(session, srUuid, dryRun=False, immediate=False):
    init(srUuid)
    sr = SR.getInstance(srUuid, session)
    if not sr.gcEnabled(False):
        return

    try:
        _gcLoop(sr, dryRun, immediate=immediate)
    finally:
        sr.check_no_space_candidates()
        sr.cleanup()
        sr.logFilter.logState()
        del sr.xapi


def _abort(srUuid, soft=False):
    """Aborts an GC/coalesce.

    srUuid: the UUID of the SR whose GC/coalesce must be aborted
    soft: If set to True and there is a pending abort signal, the function
    doesn't do anything. If set to False, a new abort signal is issued.

    returns: If soft is set to False, we return True holding lockGCActive. If
    soft is set to False and an abort signal is pending, we return False
    without holding lockGCActive. An exception is raised in case of error."""
    Util.log("=== SR %s: abort ===" % (srUuid))
    init(srUuid)
    if not lockGCActive.acquireNoblock():
        gotLock = False
        Util.log("Aborting currently-running instance (SR %s)" % srUuid)
        abortFlag = IPCFlag(srUuid)
        if not abortFlag.set(FLAG_TYPE_ABORT, soft):
            return False
        for i in range(SR.LOCK_RETRY_ATTEMPTS):
            gotLock = lockGCActive.acquireNoblock()
            if gotLock:
                break
            time.sleep(SR.LOCK_RETRY_INTERVAL)
        abortFlag.clear(FLAG_TYPE_ABORT)
        if not gotLock:
            raise util.CommandException(code=errno.ETIMEDOUT,
                    reason="SR %s: error aborting existing process" % srUuid)
    return True


def init(srUuid):
    global lockGCRunning
    if not lockGCRunning:
        lockGCRunning = lock.Lock(lock.LOCK_TYPE_GC_RUNNING, srUuid)
    global lockGCActive
    if not lockGCActive:
        lockGCActive = LockActive(srUuid)


class LockActive:
    """
    Wraps the use of LOCK_TYPE_GC_ACTIVE such that the lock cannot be acquired
    if another process holds the SR lock.
    """
    def __init__(self, srUuid):
        self._lock = lock.Lock(LOCK_TYPE_GC_ACTIVE, srUuid)
        self._srLock = lock.Lock(vhdutil.LOCK_TYPE_SR, srUuid)

    def acquireNoblock(self):
        self._srLock.acquire()

        try:
            return self._lock.acquireNoblock()
        finally:
            self._srLock.release()

    def release(self):
        self._lock.release()


##############################################################################
#
#  API
#
def abort(srUuid, soft=False):
    """Abort GC/coalesce if we are currently GC'ing or coalescing a VDI pair.
    """
    if _abort(srUuid, soft):
        Util.log("abort: releasing the process lock")
        lockGCActive.release()
        return True
    else:
        return False


def gc(session, srUuid, inBackground, dryRun=False):
    """Garbage collect all deleted VDIs in SR "srUuid". Fork & return
    immediately if inBackground=True.

    The following algorithm is used:
    1. If we are already GC'ing in this SR, return
    2. If we are already coalescing a VDI pair:
        a. Scan the SR and determine if the VDI pair is GC'able
        b. If the pair is not GC'able, return
        c. If the pair is GC'able, abort coalesce
    3. Scan the SR
    4. If there is nothing to collect, nor to coalesce, return
    5. If there is something to collect, GC all, then goto 3
    6. If there is something to coalesce, coalesce one pair, then goto 3
    """
    Util.log("=== SR %s: gc ===" % srUuid)

    signal.signal(signal.SIGTERM, receiveSignal)

    if inBackground:
        if daemonize():
            # we are now running in the background. Catch & log any errors
            # because there is no other way to propagate them back at this
            # point

            try:
                _gc(None, srUuid, dryRun)
            except AbortException:
                Util.log("Aborted")
            except Exception:
                Util.logException("gc")
                Util.log("* * * * * SR %s: ERROR\n" % srUuid)
            os._exit(0)
    else:
        _gc(session, srUuid, dryRun, immediate=True)


def start_gc(session, sr_uuid):
    """
    This function is used to try to start a backgrounded GC session by forking
    the current process. If using the systemd version, call start_gc_service() instead.
    """
    # don't bother if an instance already running (this is just an
    # optimization to reduce the overhead of forking a new process if we
    # don't have to, but the process will check the lock anyways)
    lockRunning = lock.Lock(lock.LOCK_TYPE_GC_RUNNING, sr_uuid)
    if not lockRunning.acquireNoblock():
        if should_preempt(session, sr_uuid):
            util.SMlog("Aborting currently-running coalesce of garbage VDI")
            try:
                if not abort(sr_uuid, soft=True):
                    util.SMlog("The GC has already been scheduled to re-start")
            except util.CommandException as e:
                if e.code != errno.ETIMEDOUT:
                    raise
                util.SMlog('failed to abort the GC')
        else:
            util.SMlog("A GC instance already running, not kicking")
            return
    else:
        lockRunning.release()

    util.SMlog(f"Starting GC file is {__file__}")
    subprocess.run([__file__, '-b', '-u', sr_uuid, '-g'],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)

def start_gc_service(sr_uuid, wait=False):
    """
    This starts the templated systemd service which runs GC on the given SR UUID.
    If the service was already started, this is a no-op.

    Because the service is a one-shot with RemainAfterExit=no, when called with
    wait=True this will run the service synchronously and will not return until the
    run has finished. This is used to force a run of the GC instead of just kicking it
    in the background.
    """
    sr_uuid_esc = sr_uuid.replace("-", "\\x2d")
    util.SMlog(f"Kicking SMGC@{sr_uuid}...")
    cmd=[ "/usr/bin/systemctl", "--quiet" ]
    if not wait:
        cmd.append("--no-block")
    cmd += ["start", f"SMGC@{sr_uuid_esc}"]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)


def gc_force(session, srUuid, force=False, dryRun=False, lockSR=False):
    """Garbage collect all deleted VDIs in SR "srUuid". The caller must ensure
    the SR lock is held.
    The following algorithm is used:
    1. If we are already GC'ing or coalescing a VDI pair, abort GC/coalesce
    2. Scan the SR
    3. GC
    4. return
    """
    Util.log("=== SR %s: gc_force ===" % srUuid)
    init(srUuid)
    sr = SR.getInstance(srUuid, session, lockSR, True)
    if not lockGCActive.acquireNoblock():
        abort(srUuid)
    else:
        Util.log("Nothing was running, clear to proceed")

    if force:
        Util.log("FORCED: will continue even if there are VHD errors")
    sr.scanLocked(force)
    sr.cleanupCoalesceJournals()

    try:
        sr.cleanupCache()
        sr.garbageCollect(dryRun)
    finally:
        sr.cleanup()
        sr.logFilter.logState()
        lockGCActive.release()


def get_state(srUuid):
    """Return whether GC/coalesce is currently running or not. This asks systemd for
    the state of the templated SMGC service and will return True if it is "activating"
    or "running" (for completeness, as in practice it will never achieve the latter state)
    """
    sr_uuid_esc = srUuid.replace("-", "\\x2d")
    cmd=[ "/usr/bin/systemctl", "is-active", f"SMGC@{sr_uuid_esc}"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    state = result.stdout.decode('utf-8').rstrip()
    if state == "activating" or state == "running":
        return True
    return False


def should_preempt(session, srUuid):
    sr = SR.getInstance(srUuid, session)
    entries = sr.journaler.getAll(VDI.JRN_COALESCE)
    if len(entries) == 0:
        return False
    elif len(entries) > 1:
        raise util.SMException("More than one coalesce entry: " + str(entries))
    sr.scanLocked()
    coalescedUuid = entries.popitem()[0]
    garbage = sr.findGarbage()
    for vdi in garbage:
        if vdi.uuid == coalescedUuid:
            return True
    return False


def get_coalesceable_leaves(session, srUuid, vdiUuids):
    coalesceable = []
    sr = SR.getInstance(srUuid, session)
    sr.scanLocked()
    for uuid in vdiUuids:
        vdi = sr.getVDI(uuid)
        if not vdi:
            raise util.SMException("VDI %s not found" % uuid)
        if vdi.isLeafCoalesceable():
            coalesceable.append(uuid)
    return coalesceable


def cache_cleanup(session, srUuid, maxAge):
    sr = SR.getInstance(srUuid, session)
    return sr.cleanupCache(maxAge)


def debug(sr_uuid, cmd, vdi_uuid):
    Util.log("Debug command: %s" % cmd)
    sr = SR.getInstance(sr_uuid, None)
    if not isinstance(sr, LVHDSR):
        print("Error: not an LVHD SR")
        return
    sr.scanLocked()
    vdi = sr.getVDI(vdi_uuid)
    if not vdi:
        print("Error: VDI %s not found")
        return
    print("Running %s on SR %s" % (cmd, sr))
    print("VDI before: %s" % vdi)
    if cmd == "activate":
        vdi._activate()
        print("VDI file: %s" % vdi.path)
    if cmd == "deactivate":
        ns = lvhdutil.NS_PREFIX_LVM + sr.uuid
        sr.lvmCache.deactivate(ns, vdi.uuid, vdi.fileName, False)
    if cmd == "inflate":
        vdi.inflateFully()
        sr.cleanup()
    if cmd == "deflate":
        vdi.deflate()
        sr.cleanup()
    sr.scanLocked()
    print("VDI after:  %s" % vdi)


def abort_optional_reenable(uuid):
    print("Disabling GC/coalesce for %s" % uuid)
    ret = _abort(uuid)
    input("Press enter to re-enable...")
    print("GC/coalesce re-enabled")
    lockGCRunning.release()
    if ret:
        lockGCActive.release()
