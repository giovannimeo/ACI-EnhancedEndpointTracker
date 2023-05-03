
from ... utils import get_db
from ... utils import get_redis

from .. utils import get_apic_session
from .. utils import get_attributes
from .. utils import get_class
from .. utils import get_fabric_version
from .. utils import parse_apic_version
from .. utils import raise_interrupt
from .. utils import validate_session_role
from .. subscription_ctrl import SubscriptionCtrl

from . common import BG_EVENT_HANDLER_INTERVAL
from . common import HELLO_INTERVAL
from . common import MANAGER_CTRL_CHANNEL
from . common import MANAGER_WORK_QUEUE
from . common import MAX_EPM_BUILD_TIME
from . common import MAX_SEND_MSG_LENGTH
from . common import MINIMUM_SUPPORTED_VERSION
from . common import MO_BASE
from . common import SUBSCRIBER_CTRL_CHANNEL
from . common import WATCHER_BROADCAST_CHANNEL
from . common import WORKER_BROADCAST_CHANNEL
from . common import WORKER_CTRL_CHANNEL
from . common import BackgroundThread
from . common import db_alive
from . common import get_msg_hash
from . common import get_vpc_domain_id
from . common import log_version
from . common import parse_tz
from . ept_msg import MSG_TYPE
from . ept_msg import WORK_TYPE
from . ept_msg import eptEpmEventParser
from . ept_msg import eptMsg
from . ept_msg import eptMsgBulk
from . ept_msg import eptMsgHello
from . ept_msg import eptMsgWork
from . ept_msg import eptMsgWorkDeleteEpt
from . ept_msg import eptMsgWorkRaw
from . ept_msg import eptMsgWorkStdMo
from . ept_msg import eptMsgWorkWatchNode
from . ept_epg import eptEpg
from . ept_history import eptHistory
from . ept_node import eptNode
from . ept_pc import eptPc
from . ept_queue_stats import eptQueueStats
from . ept_settings import eptSettings
from . ept_subnet import eptSubnet
from . ept_tunnel import eptTunnel
from . ept_vnid import eptVnid
from . ept_vpc import eptVpc
from . mo_dependency_map import dependency_map

from importlib import import_module
from six.moves.queue import Queue

import logging
import re
import threading
import time
import traceback

# module level logging
logger = logging.getLogger(__name__)

class eptSubscriberExitError(Exception):
    pass

class eptSubscriber(object):
    """ builds initial fabric state and subscribes to events to ensure db is in sync with fabric.
        epm events are sent to workers to analyze.
        subscriber also listens 
    """
    def __init__(self, fabric, active_workers={}):
        # receive instance of Fabric rest object along with dict of active_workers indexed by role
        self.fabric = fabric
        self.settings = eptSettings.load(fabric=self.fabric.fabric, settings="default")
        self.initializing = True    # set to queue events until fully initialized
        self.epm_initializing = True # different initializing flag for epm events
        self.stopped = False        # set to ignore events after hard_restart triggered
        self.db = None
        self.redis = None
        self.session = None
        self.bg_thread = None           # background thread used to batch epm/std_mo event messages
        self.stats_thread = None        # update stats at regular interval
        self.epm_event_queue = Queue()
        self.std_mo_event_queue = Queue()
        self.epm_parser = None  # initialized once overlay vnid is known
        self.soft_restart_ts = 0    # timestamp of last soft_restart
        self.subscription_check_interval = 5.0   # interval to check subscription health
        self.manager_ctrl_channel_lock = threading.Lock()
        self.manager_work_queue_lock = threading.Lock()
        self.queue_stats_lock = threading.Lock()

        # broadcast hello for any managers (registration and keepalives)
        self.hello_thread = None
        self.hello_msg = eptMsgHello(self.fabric.fabric, "subscriber", [], time.time())
        self.hello_msg.seq = 0

        # active workers indexed by role. Each role is a list of TrackedWorker objects.
        # Note, this is initial value of workers when subscriber is started. It may not be accurate 
        # at a later time and therefore should only be trusted during initial startup. If we want 
        # to support dynamic worker bringup/teardown in the future, then we will need to implement 
        # messaging between manager and subscriber with active worker updates to keep them in sync.
        # for now, if active workers change then subscriber will be restarted by manager process
        self.active_workers = active_workers

        # keep a dummy seq for each supported broadcast channel
        self.watcher_broadcast_seq = 0
        self.worker_broadcast_seq = 0

        # keep stats per active worker queue
        fab_id = "fab-%s" % self.fabric.fabric
        self.queue_stats = {
            WATCHER_BROADCAST_CHANNEL: eptQueueStats.load(
                proc=fab_id,
                queue=WATCHER_BROADCAST_CHANNEL,
            ),
            WORKER_BROADCAST_CHANNEL: eptQueueStats.load(
                proc=fab_id,
                queue=WORKER_BROADCAST_CHANNEL,
            ),
            WORKER_CTRL_CHANNEL: eptQueueStats.load(
                proc=fab_id,
                queue=WORKER_CTRL_CHANNEL,
            ),
            "total": eptQueueStats.load(proc=fab_id, queue="total"),
        }
        for role in self.active_workers:
            for worker in self.active_workers[role]:
                for q in worker.queues:
                    self.queue_stats[q] = eptQueueStats.load(proc=fab_id, queue=q)
        # initialize stats counters
        for k, q in self.queue_stats.items():
            q.init_queue()

        # track when fabric epm EOF was sent 
        self.epm_eof_tracking = None
        self.epm_eof_start = None

        # classes that have corresponding mo Rest object and handled by handle_std_mo_event
        # the order shouldn't matter during build but just to be safe we'll control the order...
        self.ordered_mo_classes = [
            # ordered l3out dependencies
            "fvCtx",
            "l3extRsEctx",
            "l3extOut",
            "l3extExtEncapAllocator",
            "l3extInstP",
            # ordered BD/EPG/subnet dependencies
            "fvBD",
            "fvSvcBD",
            "fvRsBd",
            "vnsRsEPpInfoToBD",
            "vnsRsLIfCtxToBD",
            "vnsLIfCtx",
            "mgmtRsMgmtBD",
            "mgmtInB",
            "fvAEPg",
            "vnsEPpInfo",
            "fvSubnet",
            "fvIpAttr",
            # pcAggr needs to be created before RsMbr interfaces
            "pcAggrIf",
            "pcRsMbrIfs",
            # no dependencies
            "tunnelIf",
            "vpcRsVpcConf",
            "datetimeFormat",
        ]
        # dict of classname to import mo object
        self.mo_classes = {}
        for mo in self.ordered_mo_classes:
            self.mo_classes[mo] = getattr(import_module(".%s" % mo, MO_BASE), mo)

        # static/special handlers for a subset of 'slow' subscriptions
        # note, slow subscriptions are handled via handle_std_mo_event and dependency_map
        self.subscription_classes = [
            "fabricProtPol",        # handle_fabric_prot_pol
            "fabricAutoGEp",        # handle_fabric_group_ep
            "fabricExplicitGEp",    # handle_fabric_group_ep
            "fabricNode",           # handle_fabric_node
        ]
        # classname to function handler for subscription events
        self.handlers = {                
            "fabricProtPol": self.handle_fabric_prot_pol,
            "fabricAutoGEp": self.handle_fabric_group_ep,
            "fabricExplicitGEp": self.handle_fabric_group_ep,
            "fabricNode": self.handle_fabric_node,
        }

        # epm subscriptions expect a high volume of events
        # note the order of the subscription classes is also the order in which analysis is performed
        #rs-ip-events before  epmIpEp so that each local epmIpEp will already have corresponding mac 
        # rewrite info ready. Ideally, all epmIpEp analysis completes in under 
        # TRANSITORY_STALE_NO_LOCAL time (300 seconds) so no false stale is triggered. 
        self.epm_subscription_classes = [
            "epmRsMacEpToIpEpAtt",
            "epmIpEp",
            "epmMacEp",
        ]

        all_interests = {}
        for s in self.subscription_classes:
            all_interests[s] = {"handler": self.handle_event}
        for s in self.mo_classes:
            all_interests[s] = {"handler": self.handle_std_mo_event}
        # wait to add epm classes
        self.subscriber = SubscriptionCtrl(
            self.fabric,
            all_interests,
            subscribe_timeout=30,
            heartbeat_timeout=self.fabric.heartbeat_timeout,
            heartbeat_interval=self.fabric.heartbeat_interval,
            heartbeat_max_retries=self.fabric.heartbeat_max_retries,
        )

    def __repr__(self):
        return "sub-%s" % self.fabric.fabric

    def run(self):
        """ wrapper around run to handle interrupts/errors """
        threading.currentThread().name = "sub-main"
        log_version()
        logger.info("starting eptSubscriber for fabric '%s'", self.fabric.fabric)
        try:
            # allocate a unique db connection as this is running in a new process
            self.db = get_db(uniq=True, overwrite_global=True, write_concern=True)
            self.redis = get_redis()
            # start hello thread
            self.hello_thread = BackgroundThread(
                func=self.send_hello,
                name="sub-hello",
                count=0,
                interval = HELLO_INTERVAL
            )
            self.hello_thread.daemon = True
            self.hello_thread.start()
            # stats thread
            self.stats_thread = BackgroundThread(
                func=self.update_stats,
                name="sub-stats",
                count=0,
                interval= eptQueueStats.STATS_INTERVAL
            )
            self.stats_thread.daemon = True
            self.stats_thread.start()
            # start background event handler thread
            self.bg_thread = BackgroundThread(
                func=self.handle_background_event_queue,
                name="sub-event",
                count=0,
                interval=BG_EVENT_HANDLER_INTERVAL
            )
            self.bg_thread.daemon = True
            self.bg_thread.start()
            self._run()
        except eptSubscriberExitError as e:
            logger.warn("subscriber exit: %s", e)
        except KeyboardInterrupt as e:
            logger.debug("keyboard interupt: %s", e)
        except (Exception, SystemExit) as e:
            logger.error("Traceback:\n%s", traceback.format_exc())
        finally:
            self.subscriber.unsubscribe()
            if self.db is not None:
                self.db.client.close()
            if self.hello_thread is not None:
                self.hello_thread.exit()
            if self.bg_thread is not None:
                self.bg_thread.exit()
            if self.stats_thread is not None:
                self.stats_thread.exit()

    def increment_stats(self, queue, tx=False, count=1):
        """ update queue stats for transmit/receive message """
        # update stats queue
        with self.queue_stats_lock:
            if queue in self.queue_stats:
                if tx:
                    self.queue_stats[queue].total_tx_msg+= count
                    self.queue_stats["total"].total_tx_msg+= count
                else:
                    self.queue_stats[queue].total_rx_msg+= count
                    self.queue_stats["total"].total_rx_msg+= count

    def update_stats(self):
        """ update stats at regular interval """
        # monitor db health prior to db updates
        if not db_alive(self.db):
            logger.error("db no longer reachable/alive")
            raise_interrupt()
            return
        # update stats at regular interval for all queues
        for k, q in self.queue_stats.items():
            with self.queue_stats_lock:
                q.collect(qlen = self.redis.llen(k))

    def send_hello(self):
        """ send hello/keepalives at regular interval to manager process """
        self.hello_msg.seq+= 1
        self.redis.publish(WORKER_CTRL_CHANNEL, self.hello_msg.jsonify())
        self.increment_stats(WORKER_CTRL_CHANNEL, tx=True)

    def broadcast(self, msg):
        """ broadcast one or more messages. Broadcast moved to pub/sub mechanism so simply need
            to publish the original message onto broadcast channel. msg must be of type eptMsgWork
            or child with role attribute to determine appropriate channel. If role is None or not
            present then msg is broadcast to all channels
            Note, broadcast does not currently use eptMsgBulk (no use case at this time...)
        """
        all_channels = [ WORKER_BROADCAST_CHANNEL, WATCHER_BROADCAST_CHANNEL]
        if not isinstance(msg, list):
            msg = [msg]
        for m in msg:
            m.fabric = self.fabric.fabric
            role = getattr(m, "role", None)
            if role == "watcher":
                channels = [ WATCHER_BROADCAST_CHANNEL ]
                self.watcher_broadcast_seq+=1
                seq = [self.watcher_broadcast_seq]
            elif role == "worker":
                channels = [ WORKER_BROADCAST_CHANNEL ] 
                self.worker_broadcast_seq+=1
                seq = [self.worker_broadcast_seq ]
            else:
                channels = all_channels
                self.watcher_broadcast_seq+=1
                self.worker_broadcast_seq+=1
                seq = [self.worker_broadcast_seq, self.watcher_broadcast_seq]
            for i, channel in enumerate(channels):
                m.seq = seq[i]
                logger.debug("broadcast [q:%s] msg: %s", channel, m)
                self.redis.publish(channel, m.jsonify())
                self.increment_stats(channel, tx=True)

    def send_msg(self, msg, prepend=False):
        """ prepare list of messages to dispatch to a worker. Messages are sent as a single message
            or via eptMsgBulk which can contain up to MAX_SEND_MSG_LENGTH messages. If the address
            is 0 then implied broadcast to all workers of role for provided msg.  Else, hash logic
            is applied to send to send specific worker based on vnid and address.
            Note, messages must be of type eptMsgWork (or inherited object) which contain addr, 
            qnum, and role.
            prepend support added to support priority-like functionality with only a single queue.
            When prepend is set to True, a lpush is executed instead of rpush to force the message
            to the top of the queue.
        """
        # dict indexed by worker_id and qnum with a tuple (worker, worker-msgs), where worker-msgs 
        # is a list of eptBulkMsg objects with at most MAX_SEND_MSG_LENGTH per bulk message
        work = {}
        if not isinstance(msg, list):
            msg = [msg]
        for m in msg:
            m.fabric = self.fabric.fabric
            if m.role not in self.active_workers or len(self.active_workers[m.role]) == 0:
                logger.warn("no available workers for role '%s'", m.role)
            else:
                _hash = get_msg_hash(m)
                worker = self.active_workers[m.role][_hash % len(self.active_workers[m.role])]
                if m.qnum >= len(worker.queues):
                    logger.warn("unable to enqueue work on worker %s, queue %s does not exist", 
                        worker.worker_id, m.qnum)
                    # force qnum to length of worker.queues assuming worker has at least one queue
                    if len(worker.queues) > 0:
                        m.qnum = len(worker.queues) - 1
                        logger.debug("overwritting queue to %s", m.qnum)
                    else:
                        logger.warn("unable to send message to worker with 0 queues")
                        continue

                if worker.worker_id not in work:
                    work[worker.worker_id] = {}
                if m.qnum not in work[worker.worker_id]:
                    work[worker.worker_id][m.qnum] = (worker, [eptMsgBulk()])
                if len(work[worker.worker_id][m.qnum][1][-1].msgs) >= MAX_SEND_MSG_LENGTH:
                    work[worker.worker_id][m.qnum][1].append(eptMsgBulk())
                work[worker.worker_id][m.qnum][1][-1].msgs.append(m)
                # increment seq number for this message and for worker
                with worker.queue_locks[m.qnum]:
                    worker.last_seq[m.qnum]+= 1
                    m.seq = worker.last_seq[m.qnum]
                self.increment_stats(worker.queues[m.qnum], tx=True)

        # at this point work is dict indexed by worker-id and queue. Each queue contains a list of
        # one or more eptMsgBulk objects that need to be transmitted individually.
        for worker_id in work:
            for qnum in work[worker_id]:
                (worker, bulk_msgs) = work[worker_id][qnum]
                for bulk in bulk_msgs:
                    send_count = len(bulk.msgs)
                    # if there's only one message, then send just that single message
                    if len(bulk.msgs) == 1:
                        bulk = bulk.msgs[0]
                    else:
                        # update bulk sequence number to last entry sent
                        bulk.seq = bulk.msgs[-1].seq
                    with worker.queue_locks[qnum]:
                        try:
                            #logger.debug("enqueue %s: %s", worker.queues[qnum], bulk)
                            if prepend:
                                self.redis.lpush(worker.queues[qnum], bulk.jsonify())
                            else:
                                self.redis.rpush(worker.queues[qnum], bulk.jsonify())
                        except Exception as e:
                            logger.debug("Traceback:\n%s", traceback.format_exc())
                            logger.error("failed to enqueue msg on queue (%s) %s: %s", e,
                                worker.queues[qnum],  bulk)

    def send_msg_direct(self, worker, msg):
        """ send one or more msgs directly to a single worker. msg must be of type eptMsgWork or 
            child with addr, qnum, and role set
        """
        if not isinstance(msg, list):
            msg = [msg]
        for m in msg:
            m.fabric = self.fabric.fabric
            if m.qnum >= len(worker.queues):
                logger.warn("unable to enqueue work on worker %s, queue %s does not exist", 
                    worker.worker_id, m.qnum)
            else:
                # increment seq number for this message and for worker
                with worker.queue_locks[m.qnum]:
                    worker.last_seq[m.qnum]+= 1
                    m.seq = worker.last_seq[m.qnum]
                self.increment_stats(worker.queues[m.qnum], tx=True)
                # send mesage
                with worker.queue_locks[m.qnum]:
                    try:
                        self.redis.rpush(worker.queues[m.qnum], m.jsonify())
                    except Exception as e:
                        logger.debug("Traceback:\n%s", traceback.format_exc())
                        logger.error("failed to enqueue msg on queue (%s) %s: %s", e, m.qnum, m)

    def handle_channel_msg(self, msg):
        """ handle msg received on subscribed channels """
        try:
            if msg["type"] == "message":
                channel = msg["channel"]
                msg = eptMsg.parse(msg["data"]) 
                logger.debug("[%s] msg on q(%s): %s", self, channel, msg)
                if channel == SUBSCRIBER_CTRL_CHANNEL:
                    self.handle_subscriber_ctrl(msg)
                else:
                    logger.warn("[%s] unsupported channel: %s", self, channel)
        except Exception as e:
            logger.debug("[%s] failed to handle msg: %s", self, msg)
            logger.error("Traceback:\n%s", traceback.format_exc())

    def handle_subscriber_ctrl(self, msg):
        """ handle subscriber control messages """
        # all subscriber ctrl messages must have fabric present
        if msg.fabric != self.fabric.fabric:
            logger.debug("request not for this fabric")
            return
        if msg.msg_type == MSG_TYPE.REFRESH_EPT:
            self.refresh_endpoint(msg.vnid, msg.addr, msg.type)
        elif msg.msg_type == MSG_TYPE.DELETE_EPT:
            # enqueue work to available worker
            self.send_msg(eptMsgWorkDeleteEpt(msg.addr, "worker", {"vnid":msg.vnid},
                WORK_TYPE.DELETE_EPT, qnum=msg.qnum,
            ))
        elif msg.msg_type == MSG_TYPE.SETTINGS_RELOAD:
            # reload local settings and send broadcast for settings reload to all workers
            logger.debug("reloading local ept settings")
            self.settings = eptSettings.load(fabric=self.fabric.fabric, settings="default")
            # node addr of 0 is broadcast to all nodes. set role to None to send to all roles
            logger.debug("broadcasting settings reload to all roles")
            self.broadcast(eptMsgWork(0, None, {}, WORK_TYPE.SETTINGS_RELOAD))
        elif msg.msg_type == MSG_TYPE.FABRIC_EPM_EOF_ACK:
            # received an ack from a worker for completion of work
            logger.debug("%s receiving EPM EOF ACK: %s", msg.fabric, msg.addr)
            if self.epm_eof_tracking is not None:
                if msg.addr in self.epm_eof_tracking:
                    self.epm_eof_tracking[msg.addr] = True
                else:
                    logger.warn("%s received ack from unknown worker %s", msg.fabric, msg.addr)
                # check if all workers have been received or still pending from any
                pending = self.get_workers_with_pending_ack()
                if len(pending) == 0:
                    logger.debug("%s received epm ack from all workers", msg.fabric)
                    # unpause and stop tracking
                    logger.debug("%s broadcasting resume to all watchers", msg.fabric)
                    self.broadcast(eptMsgWork(0,"watcher",{},WORK_TYPE.FABRIC_WATCH_RESUME))
                    self.epm_eof_tracking = None
                    self.fabric.add_fabric_event("running")
            else:
                logger.debug("%s ignoring ack as tracking is disabled", msg.fabric)
        else:
            logger.debug("%s ignoring unexpected msg type: %s", msg.fabric, msg.msg_type)

    def _run(self):
        """ monitor fabric and enqueue work to workers """

        init_str = "initializing"
        # first step is to get a valid apic session, bail out if unable to connect
        logger.debug("starting ept_subscriber session")
        # The ept_subscriber session is started by the manager and is
        # supposed to be a master session with the fabric given it
        # contains the subscriptions
        self.session = get_apic_session(self.fabric, masterSession=True)
        if self.session is None:
            logger.warn("failed to connect to fabric: %s", self.fabric.fabric)
            self.fabric.add_fabric_event("failed", "failed to connect to apic")
            return
        # validate from session that domain 'all' is present and we are running with role 'admin'
        (valid, err_msg) = validate_session_role(self.session)
        if not valid:
            self.fabric.auto_start = False
            self.fabric.add_fabric_event("failed", err_msg)
            self.session.close()
            return

        # get the apic id we connected to 
        apic_info = get_attributes(self.session, "info")
        connected_str = "connected to apic %s" % self.session.hostname
        if apic_info is None or "id" not in apic_info:
            logger.warn("unable to get topInfo for apic")
        else:
            connected_str = "connected to apic-%s, %s" % (apic_info["id"], self.session.hostname)
        self.fabric.add_fabric_event(init_str, connected_str)

        # get controller version, highlight mismatch and verify minimum version
        fabric_version = get_fabric_version(self.session)
        if len(fabric_version) == 0 or "controller" not in fabric_version:
            logger.warn("failed to determine apic version")
            self.fabric.add_fabric_event("failed", "failed to determine apic version")
            return
        apic_version = fabric_version["controller"]
        switch_version = []
        apic_version_set = set([n["version"] for n in fabric_version["controller"]])
        switch_version_set = set()
        if "switch" in fabric_version:
            switch_version_set = set([n["version"] for n in fabric_version["switch"]])
            switch_version = fabric_version["switch"]
        logger.debug("apic version set: %s, switch version set: %s", apic_version_set, 
                    switch_version_set)
        if len(apic_version_set)>1:
            logger.warn("version mismatch for %s: %s", self.fabric.fabric, apic_version_set)
            self.fabric.add_fabric_event("warning", "version mismatch: %s" % ", ".join([
                    "apic-%s: %s" % (n["node"], n["version"]) for n in apic_version
                ]))
        # use whatever the first detected version is for validation, we don't expect version 
        # mismatch for controllers so warning is sufficient
        min_version = parse_apic_version(MINIMUM_SUPPORTED_VERSION)
        version = parse_apic_version(apic_version[0]["version"])
        self.fabric.add_fabric_event(init_str, "apic version: %s, apic count: %s" % (
            apic_version[0]["version"], len(apic_version)))
        if version is None or min_version is None:
            logger.warn("failed to parse apic version: %s (min version: %s)", version, min_version)
            self.fabric.add_fabric_event("failed","unknown or unsupported apic version: %s" % (
                apic_version[0]["version"]))
            self.fabric.auto_start = False
            self.fabric.save()
            return
        # will check major/min/build and ignore patch for version check for now
        min_matched = True
        if version["major"] < min_version["major"]:
            min_matched = False
        elif version["major"] == min_version["major"]:
            if version["minor"] < min_version["minor"]:
                min_matched = False
            elif version["minor"] == min_version["minor"]:
                min_matched = (version["build"] >= min_version["build"])
        if not min_matched:
            logger.warn("fabric does not meet minimum code version (%s < %s)", version, min_version)
            self.fabric.add_fabric_event("failed","unknown or unsupported apic version: %s" % (
                apic_version[0]["version"]))
            self.fabric.auto_start = False
            self.fabric.save()
            return
        # if this is less than 4.0 then override session subscription_refresh_time to default or less
        # we also need to check against every switch.
        subscribe_check_ok = version["major"] >= 4
        for v in [parse_apic_version(sv) for sv in switch_version_set]:
            if v is not None and v["major"] < 4:
                subscribe_check_ok = False
                break
        if not subscribe_check_ok:
            if self.session.subscription_refresh_time > self.session.DEFAULT_SUBSCRIPTION_REFRESH:
                logger.info("resetting subscription refresh from %s to %s", 
                        self.session.subscription_refresh_time,
                        self.session.DEFAULT_SUBSCRIPTION_REFRESH)
                self.session.subscription_refresh_time = self.session.DEFAULT_SUBSCRIPTION_REFRESH

        # get overlay-vnid, fabricProtP (which requires hard reset on change), and tz
        overlay_attr = get_attributes(session=self.session, dn="uni/tn-infra/ctx-overlay-1")
        if overlay_attr and "scope" in overlay_attr:
            self.settings.overlay_vnid = int(overlay_attr["scope"])
            vpc_attr = get_attributes(session=self.session, dn="uni/fabric/protpol")
            tz_attr = get_attributes(session=self.session, dn="uni/fabric/format-default")
            if vpc_attr and "pairT" in vpc_attr and tz_attr and "tz" in tz_attr:
                self.settings.vpc_pair_type = vpc_attr["pairT"]
                if "displayFormat" in tz_attr and tz_attr["displayFormat"] == "utc":
                    self.settings.tz = "UTC"
                else:
                    self.settings.tz = parse_tz(tz_attr["tz"])
                logger.debug("setting timezone from %s to %s", tz_attr["tz"], self.settings.tz)
                self.settings.save()
            else:
                logger.warn("failed to determine fabricProtPol pairT: %s (using default)",vpc_attr)
        else:
            logger.warn("failed to determine overlay vnid: %s", overlay_attr)
            self.fabric.add_fabric_event("failed", "unable to determine overlay-1 vnid")
            return
      
        # trigger watch pause until initial build is complete
        logger.debug("broadcasting pause to all watchers")
        self.broadcast(eptMsgWork(0, "watcher", {}, WORK_TYPE.FABRIC_WATCH_PAUSE))

        # setup slow subscriptions to catch events occurring during build 
        if self.settings.queue_init_events:
            self.subscriber.pause(self.subscription_classes + self.ordered_mo_classes)
        if not self.subscriber.subscribe(blocking=False, session=self.session):
            # see if subscriber died which will add specific reason. If not then add generic
            # subscription error to fabric events
            try:
                if self.subscriber_is_alive():
                    self.fabric.add_fabric_event("failed",
                        "failed to start one or more subscriptions"
                    )
            except eptSubscriberExitError as e:
                pass
            return

        # build mo db first as other objects rely on it
        self.fabric.add_fabric_event(init_str, "collecting base managed objects")
        if not self.build_mo():
            # build_mo sets specific error message, no need to set a second one here
            #self.fabric.add_fabric_event("failed", "failed to collect MOs")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # build node db and vpc db
        self.fabric.add_fabric_event(init_str, "building node db")
        if not self.build_node_db():
            self.fabric.add_fabric_event("failed", "failed to build node db")
            return
        if not self.build_vpc_db():
            self.fabric.add_fabric_event("failed", "failed to build node pc to vpc db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # build tunnel db
        self.fabric.add_fabric_event(init_str, "building tunnel db")
        if not self.build_tunnel_db():
            self.fabric.add_fabric_event("failed", "failed to build tunnel db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # build vnid db along with vnsLIfCtxToBD db which relies on vnid db
        self.fabric.add_fabric_event(init_str, "building vnid db")
        if not self.build_vnid_db():
            self.fabric.add_fabric_event("failed", "failed to build vnid db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # build epg db
        self.fabric.add_fabric_event(init_str, "building epg db")
        if not self.build_epg_db():
            self.fabric.add_fabric_event("failed", "failed to build epg db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # build subnet db
        self.fabric.add_fabric_event(init_str, "building subnet db")
        if not self.build_subnet_db():
            self.fabric.add_fabric_event("failed", "failed to build subnet db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # slow objects (including std mo objects) initialization completed
        self.initializing = False
        # safe to call resume even if never paused
        self.subscriber.resume(self.subscription_classes + self.ordered_mo_classes)

        # build current epm state, start subscriptions for epm objects after query completes
        self.epm_parser = eptEpmEventParser(self.fabric.fabric, self.settings.overlay_vnid)

        # build endpoint database
        self.fabric.add_fabric_event(init_str, "getting initial endpoint state")
        if not self.build_endpoint_db():
            self.fabric.add_fabric_event("failed", "failed to build initial endpoint db")
            return
        # check if subscriptions died during previous step
        self.subscriber_is_alive() 

        # epm objects intialization completed
        self.epm_initializing = False
        # safe to call resume even if never paused
        self.subscriber.resume(self.epm_subscription_classes)

        # subscribe to subscriber events only after successfully started and initialized
        channels = {
            SUBSCRIBER_CTRL_CHANNEL: self.handle_channel_msg,
        }
        p = self.redis.pubsub(ignore_subscribe_messages=True)
        p.subscribe(**channels)
        self.subscribe_thread = p.run_in_thread(sleep_time=0.01, daemon=True)
        self.subscribe_thread.name = "sub-redis"
        logger.debug("[%s] listening for events on channels: %s", self, channels.keys())

        # send EPM EOF to all workers lowest prority queue to track when initial processing is done
        # note, this needs to be done after listern is setup on SUBSCRIBER_CTRL_CHANNEL
        self.epm_eof_start = time.time()
        self.epm_eof_tracking = {}
        for role in self.active_workers:
            if role == "worker":
                for w in self.active_workers[role]:
                    self.epm_eof_tracking[w.worker_id] = False
                    logger.debug("epm eof tracking for worker %s", w.worker_id)
                    self.send_msg_direct(
                        worker=w,
                        msg=eptMsgWork("","worker",{},WORK_TYPE.FABRIC_EPM_EOF,qnum=0),
                    )

        logger.debug("sending fabric epm eof to all workers")
        self.fabric.add_fabric_event(init_str, "building endpoint db")

        while True:
            # ensure that all subscriptions are active
            self.subscriber_is_alive()

            if self.epm_eof_tracking is not None:
                # still actively tracking workers, check if we've exceeded max build time
                ts = time.time()
                if self.epm_eof_start + MAX_EPM_BUILD_TIME <= ts:
                    pending = self.get_workers_with_pending_ack()
                    err = "epm max build time(%s) exceeded while waiting for worker[%s]" % (
                                MAX_EPM_BUILD_TIME, ",".join(pending)
                            )
                    logger.warn(err)
                    self.fabric.add_fabric_event("warning", err)
                    # unpause and stop tracking
                    logger.debug("broadcasting resume to all watchers")
                    self.broadcast(eptMsgWork(0,"watcher",{},WORK_TYPE.FABRIC_WATCH_RESUME))
                    self.epm_eof_tracking = None
                    self.fabric.add_fabric_event("running")

            # sleep for check interval
            time.sleep(self.subscription_check_interval)

    def subscriber_is_alive(self):
        """ check if subscriber is alive and raise exception if it has died """
        if not self.subscriber.is_alive():
            logger.warn("subscription no longer alive for %s", self.fabric.fabric)
            # add a fabric event with specific reason for subscriber exist if set
            if self.subscriber.failure_reason is not None:
                self.fabric.add_fabric_event("failed", self.subscriber.failure_reason)
            raise eptSubscriberExitError("subscriber is not longer alive")

    def get_workers_with_pending_ack(self):
        """ check epm_eof_start and get list of workers that have not yet sent an ack """
        pending = []
        if self.epm_eof_tracking is not None:
            for (wid, complete) in self.epm_eof_tracking.items():
                if not complete:
                    pending.append(wid)
        logger.debug("%s pending ack from %s workers: [%s]", self.fabric.fabric, len(pending), 
                        ",".join(pending))
        return pending

    def hard_restart(self, reason=""):
        """ send msg to manager for fabric restart """
        logger.warn("restarting fabric monitor '%s': %s", self.fabric.fabric, reason)
        self.fabric.add_fabric_event("restarting", reason)
        # try to kill local subscriptions first
        try:
            self.stopped = True
            self.subscriber.unsubscribe()
        except Exception as e:
            logger.debug("failed to quit subscription")
            logger.error("Traceback:\n%s", traceback.format_exc())

        reason = "restarting: %s" % reason
        data = {"fabric":self.fabric.fabric, "reason":reason}
        msg = eptMsg(MSG_TYPE.FABRIC_RESTART,data=data)
        with self.manager_ctrl_channel_lock:
            self.redis.publish(MANAGER_CTRL_CHANNEL, msg.jsonify())

    def soft_restart(self, ts=None, reason=""):
        """ soft restart sets initializing to True to block new updates along with restarting 
            slow_subscriptions.  A subset of tables are rebuilt which is much faster than a hard
            restart which requires updates to names (epg and vnid db), subnet db, and most 
            importantly endpoint db.
            The following tables are rebuilt in soft restart:
                - eptNode
                - eptTunnel
                - eptVpc
                - eptPc
        """
        logger.debug("soft restart requested: %s", reason)
        if ts is not None and self.soft_restart_ts > ts:
            logger.debug("skipping stale soft_restart request (%.3f > %.3f)",self.soft_restart_ts,ts)
            return 

        init_str = "re-initializing"
        # remove slow interests from subscriber
        self.initializing = True
        self.subscriber.remove_interest(self.subscription_classes + self.ordered_mo_classes)
        for c in self.subscription_classes:
            self.subscriber.add_interest(c, self.handle_event, paused=True)
        for c in self.mo_classes:
            self.subscriber.add_interest(c, self.handle_std_mo_event, paused=True)

        # build node db and vpc db
        self.fabric.add_fabric_event("soft-reset", reason)
        self.fabric.add_fabric_event(init_str, "building node db")
        if not self.build_node_db():
            self.fabric.add_fabric_event("failed", "failed to build node db")
            return self.hard_restart("failed to build node db")
        # need to rebuild vpc db which requires a rebuild of local mo vpcRsVpcConf mo first
        (s1, err1) = self.mo_classes["vpcRsVpcConf"].rebuild(self.fabric, session=self.session)
        if not s1:
            self.fabric.add_fabric_event("failed", err1)
            return self.hard_restart("failed to build node pc to vpc db")
        (s2, err2) = self.mo_classes["pcAggrIf"].rebuild(self.fabric, session=self.session)
        if not s2:
            self.fabric.add_fabric_event("failed", err2)
            return self.hard_restart("failed to build node pc to vpc db")
        (s3, err3) = self.mo_classes["pcRsMbrIfs"].rebuild(self.fabric, session=self.session)
        if not s3:
            self.fabric.add_fabric_event("failed", err3)
            return self.hard_restart("failed to build node pc to vpc db")

        # build tunnel db
        self.fabric.add_fabric_event(init_str, "building tunnel db")
        if not self.build_tunnel_db():
            self.fabric.add_fabric_event("failed", "failed to build tunnel db")
            return self.hard_restart("failed to build tunnel db")

        # clear appropriate caches
        self.send_flush(eptNode)
        self.send_flush(eptVpc)
        self.send_flush(eptPc)
        self.send_flush(eptTunnel)

        self.fabric.add_fabric_event("running")
        self.initializing = False
        self.subscriber.resume(self.subscription_classes + self.ordered_mo_classes)

    def send_flush(self, collection, name=None):
        """ send flush message to workers for provided collection """
        logger.debug("flush %s (name:%s)", collection._classname, name)
        # node addr of 0 is broadcast to all nodes of provided role
        data = {"cache": collection._classname, "name": name}
        self.broadcast(eptMsgWork(0, "worker", data, WORK_TYPE.FLUSH_CACHE))

    def parse_event(self, event, verify_ts=True):
        """ iterarte list of (classname, attr) objects from subscription event including _ts 
            attribute representing timestamp when event was received if verify_ts is set
        """
        try:
            if type(event) is dict: event = event["imdata"]
            for e in event:
                classname = e.keys()[0]
                if "attributes" in e[classname]:
                        attr = e[classname]["attributes"]
                        if verify_ts:
                            if "_ts" in event: 
                                attr["_ts"] = event["_ts"]
                            else:
                                attr["_ts"] = time.time()
                        yield (classname, attr)
                else:
                    logger.warn("invalid event: %s", e)
        except Exception as e:
            logger.error("Traceback:\n%s", traceback.format_exc())

    def handle_event(self, event):
        """ generic handler to call appropriate handler based on event classname
            this can also enqueue events into buffer until intialization has completed
        """
        if self.stopped:
            logger.debug("ignoring event (subscriber stopped and waiting for reset)")
            return
        if self.initializing:
            # ignore events during initializing state. If queue_init_events is enabled then 
            # subscription_ctrl is 'paused' and queueing the events for us so this should only be
            # triggered if queue_init_events is disabled in which case we are intentionally 
            # igorning the event
            logger.debug("ignoring event (in initializing state): %s", event)
            return
        logger.debug("event: %s", event)
        try:
            for (classname, attr) in self.parse_event(event):
                if classname not in self.handlers:
                    logger.warn("no event handler defined for classname: %s", classname)
                else:
                    return self.handlers[classname](classname, attr)
        except Exception as e:
            logger.error("Traceback:\n%s", traceback.format_exc())

    def handle_std_mo_event(self, event):
        """ handle standard MO subscription event. This will trigger sync_event from corresponding
            MO DependencyNode which ensures that mo objects are updated in local db and dependent
            ept objects (eptVnid, eptEpg, eptSubnet, eptVpc) are also updated. A list of updated
            ept objects is return and a flush is triggered for each to ensure workers refresh their
            cache for the objects.
        """
        if self.stopped:
            logger.debug("ignoring event (subscriber stopped and waiting for reset)")
            return
        if self.initializing:
            # ignore events during initializing state. If queue_init_events is enabled then 
            # subscription_ctrl is 'paused' and queueing the events for us so this should only be
            # triggered if queue_init_events is disabled in which case we are intentionally 
            # igorning the event
            logger.debug("ignoring event (in initializing state): %s", event)
            return
        try:
            #logger.debug("event: %s", event)
            for (classname, attr) in self.parse_event(event):
                if classname not in self.mo_classes or "dn" not in attr or "status" not in attr:
                    logger.warn("event received for unknown classname: %s, %s", classname, event)
                    continue
                # addr is a string for hashing however we will only support one watcher for now so 
                # we will statically set it to an empty string. can make this dn in the future...
                # note that integer 0 is a broadcast that is never sent as bulk.
                addr = ""
                msg = eptMsgWorkStdMo(addr, "watcher",{classname:attr}, WORK_TYPE.STD_MO)
                self.std_mo_event_queue.put(msg)
        except Exception as e:
            logger.error("Traceback:\n%s", traceback.format_exc())

    def handle_epm_event(self, event, qnum=0):
        """ handle epm events received on epm_subscription
            this will parse the epm event and create and eptMsgWorkRaw msg for the event. Then it
            will add msg to epm_event_queue for background process to batch
        """
        if self.stopped:
            logger.debug("ignoring event (subscriber stopped and waiting for reset)")
            return
        if self.epm_initializing:
            # ignore events during initializing state. If queue_init_events is enabled then 
            # subscription_ctrl is 'paused' and queueing the events for us so this should only be
            # triggered if queue_init_events is disabled in which case we are intentionally 
            # igorning the event
            logger.debug("ignoring event (in epm_initializing state): %s", event)
            return
        try:
            for (classname, attr) in self.parse_event(event):
                # raw ept message extracting address string purely from dn of object
                # dn for each possible epm event:
                #   .../db-ep/mac-00:AA:00:00:28:1A
                #   .../db-ep/ip-[10.1.55.220]
                #   rsmacEpToIpEpAtt-.../db-ep/ip-[10.1.1.74]]
                # addr = re.sub("[\[\]]","", attr["dn"].split("-")[-1])
                #msg = eptMsgWorkRaw(addr,"worker", {classname:attr}, WORK_TYPE.RAW, qnum=qnum)
                # OR, full parse of event in subscriber module which extracts all required info 
                # this is needed for address and vnid info for hash module
                msg = self.epm_parser.parse(classname, attr, attr["_ts"])
                self.epm_event_queue.put(msg)
        except Exception as e:
            logger.error("Traceback:\n%s", traceback.format_exc())

    def handle_background_event_queue(self):
        """ pull off all current msgs in epm_event_queue/std_mo_event_queue and send as a batch.
            The purpose of this is to create bulk eptMsg objects to improve redis performance
        """
        for q in [self.std_mo_event_queue, self.epm_event_queue]:
            msgs = []
            while not q.empty():
                msgs.append(q.get())
            if len(msgs)>0:
                self.send_msg(msgs)

    def build_mo(self):
        """ build managed objects for defined classes """
        for mo in self.ordered_mo_classes:
            (success, errmsg) = self.mo_classes[mo].rebuild(self.fabric, session=self.session)
            if not success:
                self.fabric.add_fabric_event("failed", errmsg)
                return False
        return True

    def initialize_ept_collection(self, eptObject, mo_classname, attribute_map=None, 
            regex_map=None ,set_ts=False, flush=False):
        """ initialize ept collection.  Note, mo_object or mo_classname must be provided
                eptObject = eptNode, eptVnid, eptEpg, etc...
                mo_classname = classname of mo used for query, or if exists within self.mo_classes,
                                the mo object from local database
                set_ts = boolean to set modify ts within ept object. If mo_object is set, then ts
                            from mo object is written to ept object. Else, timestamp of APIC query
                            is used
                flush = boolean to flush ept collection at initialization
                attribute_map = dict handling mapping of ept attribute to mo attribute. If omitted,
                        then the attribute map will use the value from the corresponding 
                        DependencyNode (if found within the dependency_map)
                regex_map = dict - ept attribute names in regex map will contain a regex used to 
                        extract the value from the corresponding mo attribute.  if omitted, then 
                        will use the regex_map definied within the corresponding DependencyNode
                        (if found within the dependency_map)

                        This regex must contain a named capture group of 'value'.  For example:
                        attribute_map = {
                            "node": "dn"        # set's the ept value of 'node' to the mo 'dn'
                        }
                        regex_map = {
                            "node": "node-(?P<value>[0-9]+)/" # extract interger value from 'node'
                        }

            return bool success

        """
        # iterator over data from class query returning just dict attributes
        def raw_iterator(data):
            for obj in data:
                if obj is not None:
                    for attr in get_attributes(data=obj):
                        yield attr
                else:
                    logger.warn("obj is none on streaming class query for %s", eptObject)
                    return

        # iterator over mo objects returning just dict attributes
        def mo_iterator(objects):
            for o in objects:
                yield o.to_json()

        # get data from local mo db
        if mo_classname in self.mo_classes:
            data = self.mo_classes[mo_classname].find(fabric=self.fabric.fabric)
            iterator = mo_iterator
        else:
            data = get_class(self.session, mo_classname, orderBy="%s.dn"%mo_classname, stream=True)
            if data is None:
                logger.warn("failed to get data for classname %s", mo_classname)
                return False
            iterator = raw_iterator

        # get attribute_map and regex_map from arguments or dependency map
        default_attribute_map = {}
        default_regex_map = {}
        if mo_classname in dependency_map:
            default_attribute_map = dependency_map[mo_classname].ept_attributes
            default_regex_map = dependency_map[mo_classname].ept_regex_map
        if attribute_map is None: 
            attribute_map = default_attribute_map
        if regex_map is None:
            regex_map = default_regex_map

        # if attribute map is empty then it wasn't provided or no corresponding entry within the 
        # dependency map
        if len(attribute_map) == 0:
            logger.error("no attribute map found/provided for %s", mo_classname)

        ts = time.time()
        bulk_objects = []
        # iterate over results 
        for attr in iterator(data):
            db_obj = {}
            for db_attr, o_attr in attribute_map.items():
                # can only map 'plain' string attributes (not list referencing parent objects)
                if isinstance(o_attr, basestring) and o_attr in attr:
                    # check for regex_map
                    if db_attr in regex_map:
                        r1 = re.search(regex_map[db_attr], attr[o_attr])
                        if r1:
                            if "value" in r1.groupdict():
                                db_obj[db_attr] = r1.group("value")
                            else: 
                                db_obj[attr] = attr[o_attr]
                        else:
                            logger.warn("%s value %s does not match regex %s", o_attr,attr[o_attr], 
                                regex_map[db_attr])
                            db_obj = {}
                            break
                    else:
                        db_obj[db_attr] = attr[o_attr]
            if len(db_obj)>0:
                db_obj["fabric"] = self.fabric.fabric
                if set_ts: 
                    if "ts" in attr:
                        db_obj["ts"] = attr["ts"]
                    else:
                        db_obj["ts"] = ts
                bulk_objects.append(eptObject(**db_obj))
            else:
                logger.warn("%s object not added from MO (no matching attributes): %s", 
                    eptObject._classname, attr)

        # flush right before insert to minimize time of empty table
        if flush:
            logger.debug("flushing %s entries for fabric %s",eptObject._classname,self.fabric.fabric)
            eptObject.delete(_filters={"fabric":self.fabric.fabric})
        if len(bulk_objects)>0:
            eptObject.bulk_save(bulk_objects, skip_validation=False)
        else:
            logger.debug("no objects of %s to insert", mo_classname)
        return True
    
    def build_node_db(self):
        """ initialize node collection and vpc nodes. return bool success """
        logger.debug("initializing node db")
        if not self.initialize_ept_collection(eptNode, "fabricNode", attribute_map = {
                "addr": "address",
                "name": "name",
                "node": "id",
                "pod_id": "dn",
                "role": "role",
            }, regex_map = {
                "pod_id": "topology/pod-(?P<value>[0-9]+)/node-[0-9]+",
            }, flush=True):
            logger.warn("failed to build node db from fabricNode")
            return False

        # maintain list of all nodes for id to addr lookup 
        all_nodes = {}
        for n in eptNode.find(fabric=self.fabric.fabric):
            all_nodes[n.node] = n

        # extra check here, fail if no eptNode objects were found. This is either a setup with no
        # nodes present (just an APIC) or a build problem has occurred
        if len(all_nodes) == 0:
            logger.warn("no eptNode objects discovered")
            return False

        # cross reference fabricNode (which includes inactive nodes) with topSystem which includes
        # active nodes and accurate TEP for active nodes only.  Then merge firmware version
        data1 = get_class(self.session, "topSystem")
        data2 = get_class(self.session, "firmwareRunning")
        if data1 is None or data2 is None or len(data1) == 0 or len(data2) == 0:
            logger.warn("failed to read topSystem/firmwareRunning")
            return False
        for obj in data1:
            attr = obj[obj.keys()[0]]["attributes"]
            if "id" in attr and "address" in attr and "state" in attr:
                node_id = int(attr["id"])
                if node_id in all_nodes:
                    all_nodes[node_id].addr = attr["address"]
                    all_nodes[node_id].state = attr["state"]
                else:
                    logger.warn("ignorning unknown topSystem node id '%s'", node_id)
            else:
                logger.warn("invalid topSystem object (missing id or address): %s", attr)
        for obj in data2:
            attr = obj[obj.keys()[0]]["attributes"]
            if "dn" in attr and "peVer" in attr:
                r1 = re.search("topology/pod-[0-9]+/node-(?P<node_id>[0-9]+)", attr["dn"])
                if r1 is not None:
                    node_id = int(r1.group("node_id"))
                    if node_id in all_nodes:
                        all_nodes[node_id].version = attr["peVer"]
                    else:
                        logger.warn("ignoring unknown firmwareRunning node id %s", attr["dn"])
                else:
                    logger.warn("failed to parse node id from firmwareRunning dn %s", attr["dn"])
            else:
                logger.warn("invalid firmwareRunning object (missing dn or peVer): %s", attr)

        # create pseudo node for each vpc group from fabricAutoGEp and fabricExplicitGEp each of 
        # which contains fabricNodePEp
        vpc_type = "fabricExplicitGEp"
        node_ep = "fabricNodePEp"
        data = get_class(self.session, vpc_type, rspSubtree="full", rspSubtreeClass=node_ep)
        if data is None or len(data) == 0:
            logger.debug("no vpcs found for fabricExplicitGEp, checking fabricAutoGEp")
            vpc_type = "fabricAutoGEp"
            data = get_class(self.session, vpc_type, rspSubtree="full", rspSubtreeClass=node_ep)
            if data is None or len(data) == 0:
                logger.debug("no vpc configuration found")
                data = []

        # build all known vpc groups and set peer values with existing eptNodes that are members
        # of a vpc domain
        for obj in data:
            if vpc_type in obj and "attributes" in obj[vpc_type]:
                attr = obj[vpc_type]["attributes"]
                if "virtualIp" in attr and "name" in attr and "dn" in attr:
                    name = attr["name"]
                    addr = re.sub("/[0-9]+$", "", attr["virtualIp"])
                    # get children node_ep (expect exactly 2)
                    child_nodes = []
                    if "children" in obj[vpc_type]:
                        for cobj in obj[vpc_type]["children"]:
                            if node_ep in cobj and "attributes" in cobj[node_ep]:
                                cattr = cobj[node_ep]["attributes"]
                                if "id" in cattr and "peerIp" in cattr:
                                    peer_ip = re.sub("/[0-9]+$", "", cattr["peerIp"])
                                    node_id = int(cattr["id"])
                                    if node_id in all_nodes:
                                        child_nodes.append(all_nodes[node_id])
                                    else:
                                        logger.warn("unknown node id %s in %s", node_id, vpc_type)
                                else:
                                    logger.warn("invalid %s object: %s", node_ep, cobj)
                    if len(child_nodes) == 2:
                        vpc_domain_id = get_vpc_domain_id(
                            child_nodes[0].node,
                            child_nodes[1].node,
                        )
                        child_nodes[0].peer = child_nodes[1].node
                        child_nodes[1].peer = child_nodes[0].node
                        all_nodes[vpc_domain_id] = eptNode(fabric=self.fabric.fabric,
                            addr=addr,
                            name=name,
                            node=vpc_domain_id,
                            pod_id=child_nodes[0].pod_id,
                            role="vpc",
                            state="in-service",
                            nodes=[
                                {
                                    "node": child_nodes[0].node,
                                    "addr": child_nodes[0].addr,
                                },
                                {
                                    "node": child_nodes[1].node,
                                    "addr": child_nodes[1].addr,
                                },
                            ],
                        )
                    else:
                        logger.warn("expected 2 %s child objects: %s", node_ep,obj)
                else:
                    logger.warn("invalid %s object: %s", vpc_type, obj)
        
        # all nodes should have been updated (TEP info, version, and vpc updates)
        eptNode.bulk_save([all_nodes[n] for n in all_nodes], skip_validation=False)
        return True

    def build_tunnel_db(self):
        """ initialize tunnel db. return bool success """
        logger.debug("initializing tunnel db")
        if not self.initialize_ept_collection(eptTunnel, "tunnelIf", attribute_map={
                "name": "dn",
                "node": "dn",
                "intf": "id",
                "dst": "dest",
                "src": "src",
                "status": "operSt",
                "encap": "tType",
                "flags": "type",
            }, regex_map = {
                "node": "topology/pod-[0-9]+/node-(?P<value>[0-9]+)/",
                "src": "(?P<value>[^/]+)(/[0-9]+)?",
            }, flush=True, set_ts=True):
            return False

        # walk through each tunnel and map remote to correct node id with pseudo vpc node awareness
        # maintain list of all_nodes indexed by addr for quick tunnel dst lookup
        all_nodes = {}
        for n in eptNode.find(fabric=self.fabric.fabric):
            all_nodes[n.addr] = n
        bulk_objects = []
        for t in eptTunnel.find(fabric=self.fabric.fabric):
            if t.dst in all_nodes:
                t.remote = all_nodes[t.dst].node
                bulk_objects.append(t)
            else:
                # tunnel type of vxlan (instead of ivxlan), or flags of dci(multisite)/golf/mcast or 
                # proxy(spines) can be safely ignored, else print a warning
                if t.encap == "vxlan" or "proxy" in t.flags or "dci" in t.flags or \
                    "golf" in t.flags or "fabric-ext" in t.flags or "underlay-mcast" in t.flags:
                    #logger.debug("failed to map tunnel to remote node: %s", t)
                    pass
                # if we are unable to map the tunnel for a spine, that is also ok to ignore
                elif t.src in all_nodes and all_nodes[t.src].role == "leaf":
                    logger.warn("failed to map tunnel for leaf to remote node: %s", t)

        if len(bulk_objects)>0:
            eptTunnel.bulk_save(bulk_objects, skip_validation=False)

        return True

    def build_vpc_db(self):
        """ build port-channel to vpc interface mapping along with port-chhanel to name mapping.
            return bool success 
        """
        logger.debug("initializing pc/vpc db")
        # vpcRsVpcConf exists within self.mo_classses and already defined in dependency_map
        if  self.initialize_ept_collection(eptVpc,"vpcRsVpcConf",set_ts=True, flush=True) and \
            self.initialize_ept_collection(eptPc,"pcAggrIf",set_ts=True, flush=True):
            # need to build member interfaces for eptPc from pcRsMbrIfs. Maintain a list of member
            # interfaces keyed by parent. Then walk through all eptPc objects and add member.
            logger.debug("bulding eptPc member interfaces from pcRsMbrfIfs")
            pcs = {}
            for mo in self.mo_classes["pcRsMbrIfs"].find(fabric=self.fabric.fabric):
                if mo.parent not in pcs:
                    pcs[mo.parent] = []
                pcs[mo.parent].append(mo.tSKey)
            bulk_objects = []
            for pc in eptPc.find(fabric=self.fabric.fabric):
                if pc.name in pcs:
                    pc.members = pcs[pc.name]
                    bulk_objects.append(pc)
            if len(bulk_objects)>0:
                eptPc.bulk_save(bulk_objects, skip_validation=True)
            return True
        else:
            return False

    def build_vnid_db(self):
        """ initialize vnid database. return bool success
            vnid objects include the following:
                fvCtx (vrf)
                fvBD (BD)
                fvSvcBD (copy-service BD)
                l3extExtEncapAllocator (external BD)
        """
        logger.debug("initializing vnid db")
       
        # handle fvCtx, fvBD, and fvSvcBD
        logger.debug("bulding vnid from fvCtx")
        if not self.initialize_ept_collection(eptVnid, "fvCtx", set_ts=True, flush=True):
            logger.warn("failed to initialize vnid db for fvCtx")
            return False
        logger.debug("bulding vnid from fvBD")
        if not self.initialize_ept_collection(eptVnid, "fvBD", set_ts=True, flush=False):
            logger.warn("failed to initialize vnid db for fvBD")
            return False
        logger.debug("bulding vnid from fvSvcBD")
        if not self.initialize_ept_collection(eptVnid, "fvSvcBD", set_ts=True, flush=False):
            logger.warn("failed to initialize vnid db for fvSvcBD")
            return False

        # dict of name (vrf/bd) to vnid for quick lookup
        logger.debug("bulding vnid from l3extExtEncapAllocator")
        ts = time.time()
        bulk_objects = []
        vnids = {}  
        l3ctx = {}     # mapping of l3out name to vrf vnid
        for v in eptVnid.find(fabric=self.fabric.fabric): 
            vnids[v.name] = v.vnid
        for c in self.mo_classes["l3extRsEctx"].find(fabric=self.fabric.fabric):
            if c.tDn in vnids:
                l3ctx[c.parent]  = vnids[c.tDn]
            else:
                logger.warn("failed to map l3extRsEctx tDn(%s) to vrf vnid", c.tDn)
        for obj in self.mo_classes["l3extExtEncapAllocator"].find(fabric=self.fabric.fabric):
            new_vnid = eptVnid(
                fabric = self.fabric.fabric,
                vnid = int(re.sub("vxlan-","", obj.extEncap)),
                name = obj.dn,
                encap = obj.encap,
                external = True,
                ts = ts
            )
            if obj.parent in l3ctx:
                new_vnid.vrf = l3ctx[obj.parent]
            else:
                logger.warn("failed to map l3extOut(%s) to vrf vnid", obj.parent)
            bulk_objects.append(new_vnid)

        if len(bulk_objects)>0:
            eptVnid.bulk_save(bulk_objects, skip_validation=False)
        return True

    def build_epg_db(self):
        """ initialize epg database. return bool success
            epg objects include the following (all instances of fvEPg)
                fvAEPg      - normal epg            (fvRsBd - map to fvBD)
                mgmtInB     - inband mgmt epg       (mgmtRsMgmtBD - map to fvBD)
                vnsEPpInfo  - epg from l4 graph     (vnsRsEPpInfoToBD - map to fvBD)
                l3extInstP  - external epg          (no BD)
        """
        logger.debug("initializing epg db")
        flush = True
        for c in ["fvAEPg", "mgmtInB", "vnsEPpInfo", "l3extInstP"]:
            if not self.initialize_ept_collection(eptEpg, c, set_ts=True, flush=flush):
                logger.warn("failed to initialize epg db from %s", c)
                return False
            # only flush on first table
            flush = False

        logger.debug("mapping epg to bd vnid")
        # need to build mapping of epg to bd. to do so need to get the dn of the BD for each epg
        # and then lookup into vnids table for bd name to get bd vnid to merge into epg table
        bulk_object_keys = {}   # dict to prevent duplicate addition of object to bulk_objects
        bulk_objects = []
        vnids = {}      # indexed by bd/vrf name (dn), contains only vnid
        epgs = {}       # indexed by epg name (dn), contains full object
        for v in eptVnid.find(fabric=self.fabric.fabric): 
            vnids[v.name] = v.vnid
        for e in eptEpg.find(fabric=self.fabric.fabric):
            epgs[e.name] = e
        for classname in ["fvRsBd", "vnsRsEPpInfoToBD", "mgmtRsMgmtBD"]:
            logger.debug("map epg bd vnid from %s", classname)
            for mo in self.mo_classes[classname].find(fabric=self.fabric.fabric):
                epg_name = re.sub("/(rsbd|rsEPpInfoToBD|rsmgmtBD)$", "", mo.dn)
                bd_name = mo.tDn 
                if epg_name not in epgs:
                    logger.warn("cannot map bd to unknown epg '%s' from '%s'", epg_name, classname)
                    continue
                if bd_name not in vnids:
                    logger.warn("cannot map epg %s to unknown bd '%s'", epg_name, bd_name)
                    continue
                epgs[epg_name].bd = vnids[bd_name]
                if epg_name not in bulk_object_keys:
                    bulk_object_keys[epg_name] = 1
                    bulk_objects.append(epgs[epg_name])
                else:
                    logger.warn("skipping duplicate dn: %s", epg_name)

        if len(bulk_objects)>0:
            # only adding vnid here which was validated from eptVnid so no validation required
            eptEpg.bulk_save(bulk_objects, skip_validation=True)
        return True

    def build_subnet_db(self):
        """ build subnet db 
            Only two objects that we care about but they can come from a few different places:
                - fvSubnet
                    - fvBD, fvAEPg
                      vnsEPpInfo and vnsLIfCtx where the latter requires vnsRsLIfCtxToBD lookup
                - fvIpAttr
                    - fvAEPg
        """
        logger.debug("initializing subnet db")

        # use subnet dn as lookup into vnid and epg table to determine corresponding bd vnid
        # yes, we're doing duplicate db lookup as build_epg_db but db lookup on init is minimum
        # performance hit even with max scale
        vnids = {}
        epgs = {}
        for v in eptVnid.find(fabric=self.fabric.fabric): 
            vnids[v.name] = v.vnid
        for e in eptEpg.find(fabric=self.fabric.fabric):
            # we only care about the bd vnid, only add to epgs list if a non-zero value is present
            if e.bd != 0: epgs[e.name] = e.bd
        # although not technically an epg, eptVnsLIfCtxToBD contains a mapping to bd that we need
        for mo in self.mo_classes["vnsRsLIfCtxToBD"].find(fabric=self.fabric.fabric):
            if mo.tDn in vnids:
                epgs[mo.parent] = vnids[mo.tDn]
            else:
                logger.warn("%s tDn %s not in vnids", mo._classname, mo.tDn)

        bulk_objects = []
        # should now have all objects that would contain a subnet 
        for classname in ["fvSubnet", "fvIpAttr"]:
            for mo in self.mo_classes[classname].find(fabric=self.fabric.fabric):
                # usually in bd so check vnid first and then epg
                bd_vnid = None
                if mo.parent in vnids:
                    bd_vnid = vnids[mo.parent]
                elif mo.parent in epgs:
                    bd_vnid = epgs[mo.parent]
                if bd_vnid is not None:
                    # FYI - we support fvSubnet on BD and EPG for shared services so duplicate ip
                    # can exist. unique index is disabled on eptSubnet to support this... 
                    bulk_objects.append(eptSubnet(
                        fabric = self.fabric.fabric,
                        bd = bd_vnid,
                        name = mo.dn,
                        ip = mo.ip,
                        ts = mo.ts
                    ))
                else:
                    logger.warn("failed to map subnet '%s' (%s) to a bd", mo.ip, mo.parent)

        logger.debug("flushing entries in %s for fabric %s",eptSubnet._classname,self.fabric.fabric)
        eptSubnet.delete(_filters={"fabric":self.fabric.fabric})
        if len(bulk_objects)>0:
            eptSubnet.bulk_save(bulk_objects, skip_validation=False)
        return True

    def handle_fabric_prot_pol(self, classname, attr):
        """ if pairT changes in fabricProtPol then trigger hard restart """
        logger.debug("handle fabricProtPol event: %s", attr["pairT"])
        if "pairT" in attr and attr["pairT"] != self.settings.vpc_pair_type:
            msg="fabricProtPol changed from %s to %s" % (self.settings.vpc_pair_type,attr["pairT"])
            logger.warn(msg)
            self.hard_restart(msg)
        else:
            logger.debug("no change in fabricProtPol")

    def handle_fabric_group_ep(self, classname, attr):
        """ fabricExplicitGEp or fabricAutoGEp update requires unconditional soft restart """
        logger.debug("handle %s event", classname)
        self.soft_restart(ts=attr["_ts"], reason="(%s) vpc domain update" % classname)

    def handle_fabric_node(self, classname, attr):
        """ handle events for fabricNode
            If a new leaf becomes active then trigger a hard restart to rebuild endpoint database
            as there's no way of knowing when endpoint events were missed on the new node 
            If an existing leaf becomes inactive, then create delete jobs for all endpoint learns 
            for this leaf.
            If name changed, then update corresponding eptNode object (no flush required)
        """
        logger.debug("handle fabricNode event: %s", attr["dn"])
        if "dn" in attr:
            r1 = re.search("topology/pod-(?P<pod>[0-9]+)/node-(?P<node>[0-9]+)", attr["dn"])
            status = attr.get("fabricSt",None)
            name = attr.get("name", None)
            if r1 is None:
                logger.warn("failed to extract node id from fabricNode dn: %s", attr["dn"])
                return
            # get db entry for this node
            node_id = int(r1.group("node"))
            node = eptNode.load(fabric=self.fabric.fabric, node=node_id)
            if name is not None and node.exists() and node.name != name:
                # update node name
                logger.debug("node %s name updated from %s to %s", node_id, node.name, name)
                node.name = name
                node.save()
            if status is not None:
                if node.exists():
                    if node.role != "leaf":
                        logger.debug("ignoring fabricNode event for '%s'", node.role)
                    else:
                        # if this is an active event, then trigger a hard restart else trigger pseudo
                        # delete jobs for all previous entries on node.  This includes XRs to account
                        # for bounce along with generally cleanup of node state.
                        if status == "active":
                            # TODO - perform soft reset and per node epm query instead of full reset
                            self.hard_restart(reason="leaf '%s' became active" % node.node)
                        else:
                            logger.debug("node %s '%s', sending watch_node event", node.node,status)
                            msg = eptMsgWorkWatchNode("1","watcher",{},WORK_TYPE.WATCH_NODE)
                            msg.node = node.node
                            msg.ts = attr["_ts"]
                            msg.status = status
                            self.send_msg(msg)
                else:
                    if status != "active":
                        logger.debug("ignorning '%s' event for unknown node: %s",status,node_id)
                    else:
                        # a new node became active, double check that is a leaf and if so trigger a 
                        # hard restart
                        new_node_dn = "topology/pod-%s/node-%s" % (r1.group("pod"),node_id)
                        new_attr = get_attributes(session=self.session, dn=new_node_dn)
                        if new_attr is not None and "role" in new_attr and new_attr["role"]=="leaf":
                            self.hard_restart(reason="new leaf '%s' became active"%node_id)
                        else:
                            logger.debug("ignorning active event for non-leaf")
        else:
            logger.debug("ignoring fabricNode event (fabricSt or dn not present in attributes)")

    def build_endpoint_db(self):
        """ all endpoint events (eptHistory and eptEndpoint) are handled by app workers. To build
            the initial database we need to simulate create or delete events for each endpoint 
            returned from the APIC and send through worker process.  Delete jobs are created for
            endpoints previously within the database but not returned on query, all other objects 
            will result in a create job.

            Return boolean success
        """
        logger.debug("initialize endpoint db")
        start_time = time.time()

        # 3-level dict to track endpoints returned from class query endpoints[node][vnid][addr] = 1
        endpoints = {}
        # we will start epm subscription AFTER get_class (which can take a long time) but before 
        # processing endpoints.  This minimizes amount of time we lose data without having to buffer
        # all events that are recieved during get requests.
        paused = self.settings.queue_init_epm_events
        total_create = 0
        for c in self.epm_subscription_classes:
            if c == "epmRsMacEpToIpEpAtt":
                gen = get_class(self.session, c, stream=True, orderBy="%s.dn" % c)
            else:
                gen = get_class(self.session, c, stream=True, orderBy="%s.addr" % c)
            ts = time.time()
            create_count = 0
            create_msgs = []
            if not self.subscriber.add_interest(c, self.handle_epm_event, paused=paused):
                logger.warn("failed to add interest %s to subscriber", c)
                return False
            for obj in gen:
                if obj is None:
                    logger.error("failed to get epm data for class %s", c)
                    return False
                if c in obj and "attributes" in obj[c]:
                    msg = self.epm_parser.parse(c, obj[c]["attributes"], ts)
                    if msg is not None:
                        create_count+=1
                        create_msgs.append(msg)
                        if msg.node not in endpoints: endpoints[msg.node] = {}
                        if msg.vnid not in endpoints[msg.node]: endpoints[msg.node][msg.vnid] = {}
                        endpoints[msg.node][msg.vnid][msg.addr] = 1
                        # process the data now as we can't afford buffer all msgs in memory on 
                        # scale setups.
                        if len(create_msgs) >= MAX_SEND_MSG_LENGTH:
                            logger.debug("build_endpoint_db sending %s create for %s", 
                                    len(create_msgs), c)
                            self.send_msg(create_msgs)
                            create_msgs = []
                else:
                    logger.warn("invalid %s object: %s", c, obj)

            # send remaining create messages
            if len(create_msgs) > 0:
                logger.debug("build_endpoint_db sending %s create for %s", len(create_msgs),c)
                self.send_msg(create_msgs)
                create_msgs = []
            # print total for reference
            logger.debug("build_endpoint_db total %s create for %s", create_count, c)
            total_create+= create_count

        # stream delete jobs
        delete_count = 0
        delete_msgs = []
        for obj in self.get_epm_delete_msgs(endpoints):
            delete_count+= 1
            delete_msgs.append(obj)
            if len(delete_msgs) >= MAX_SEND_MSG_LENGTH:
                logger.debug("build_endpoint_db sending %s delete jobs", len(delete_msgs))
                self.send_msg(delete_msgs)
                delete_msgs = []
        # send remaining delete messages
        if len(delete_msgs) > 0:
            logger.debug("build_endpoint_db sending %s delete jobs", len(delete_msgs))
            self.send_msg(delete_msgs)
            delete_msgs = []
        # print total for reference
        logger.debug("build_endpoint_db total %s delete jobs", delete_count)
        logger.debug("build_endpoint_db total time: %.3f", time.time()-start_time)
        # add fabric event so user is aware of number of create/delete events that will be processed
        overview = "analyzing %s endpoint records" % (total_create + delete_count)
        self.fabric.add_fabric_event("initializing", overview)
        return True

    def refresh_endpoint(self, vnid, addr, addr_type):
        """ perform endpoint refresh. This triggers an API query for epmDb filtering on provided
            addr and vnid. The results are fed through handle_epm_event which is enqueued onto 
            a worker. Since workers only have a single queue, will need to insert at the top of
            thier queue.
        """
        logger.debug("refreshing [0x%06x %s]", vnid, addr)
        if addr_type == "mac":
            classname = "epmMacEp"
            kwargs = {
                "queryTargetFilter": "eq(epmMacEp.addr,\"%s\")" % addr  
            }
        else:
            # need epmIpEp and epmRsMacEpToIpEpAtt objects for this addr
            classname = "epmDb"
            f="or(eq(epmIpEp.addr,\"%s\"),wcard(epmRsMacEpToIpEpAtt.dn,\"ip-\[%s\]\"))"%(addr,addr)
            kwargs = {
                "queryTarget": "subtree",
                "targetSubtreeClass": "epmIpEp,epmRsMacEpToIpEpAtt",
                "queryTargetFilter": f
            }
        objects = get_class(self.session, classname, **kwargs)
        ts = time.time()
        # 3-level dict to track endpoints returned from class query endpoints[node][vnid][addr] = 1
        endpoints = {}
        # queue all the events to send at one time...
        create_msgs = []
        # queue all the events to send at one time...
        if objects is not None:
            for obj in objects:
                classname = obj.keys()[0]
                if "attributes" in obj[classname]:
                    attr = obj[classname]["attributes"]
                    attr["_ts"] = ts
                    msg = self.epm_parser.parse(classname, attr, attr["_ts"])
                    if msg is not None:
                        create_msgs.append(msg)
                        if msg.node not in endpoints:
                            endpoints[msg.node] = {}
                        if msg.vnid not in endpoints[msg.node]:
                            endpoints[msg.node][msg.vnid] = {}
                        endpoints[msg.node][msg.vnid][msg.addr] = 1
                else:
                    logger.debug("ignoring invalid epm object %s", obj)
            # get delete jobs
            delete_msgs = [_ for _ in self.get_epm_delete_msgs(endpoints, addr=addr, vnid=vnid)]
            logger.debug("sending %s create and %s delete msgs from refresh", len(create_msgs),
                    len(delete_msgs))
            # set force flag on each msg to trigger analysis update
            for msg in create_msgs+delete_msgs:
                msg.force = True

            self.send_msg(create_msgs+delete_msgs, prepend=True)
        else:
            logger.debug("failed to get epm objects")

    def get_epm_delete_msgs(self, endpoints, addr=None, vnid=None):
        """ from provided create endpoint dict and flt, stream iterators for epm delete msgs
            endpoints must be 3-level dict in the form endpoints[node][vnid][addr]
        """
        logger.debug("get epm delete messages (flt addr:%s, vnid:%s)", addr, vnid)

        # get entries in db and create delete events for those not deleted and not in class query.
        # we need to iterate through the results and do so as fast as possible and current rest
        # class does not support an iterator.  Therefore, using direct db call...
        projection = {
            "node": 1,
            "vnid": 1,
            "addr": 1,
            "type": 1,
        }
        flt = {
            "fabric": self.fabric.fabric,
            "events.0.status": {"$ne": "deleted"},
        }
        if addr is not None and vnid is not None:
            flt["addr"] = addr
            flt["vnid"] = vnid

        ts = time.time()
        for obj in self.db[eptHistory._classname].find(flt, projection):
            # if in endpoints dict, then stil exists in the fabric so do not create a delete event
            if obj["node"] in endpoints and obj["vnid"] in endpoints[obj["node"]] and \
                obj["addr"] in endpoints[obj["node"]][obj["vnid"]]:
                continue
            if obj["type"] == "mac":
                msg = self.epm_parser.get_delete_event("epmMacEp", obj["node"], 
                    obj["vnid"], obj["addr"], ts)
                if msg is not None:
                    yield msg
            else:
                # create an epmRsMacEpToIpEpAtt and epmIpEp delete event
                msg = self.epm_parser.get_delete_event("epmRsMacEpToIpEpAtt", obj["node"], 
                    obj["vnid"], obj["addr"], ts)
                if msg is not None:
                    yield msg
                msg = self.epm_parser.get_delete_event("epmIpEp", obj["node"], 
                    obj["vnid"], obj["addr"], ts)
                if msg is not None:
                    yield msg

