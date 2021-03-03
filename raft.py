from datetime import datetime, timedelta
import json
import os
import random
from threading import RLock
from scheduler import Scheduler
from store import create_if_not_exists
import requests
import logging

HEARBEAT_TIMEOUT = 5000
HOUSEKEEPING_TIMEOUT = 2 * 60 * 1000
HOUSEKEEPING_MAX_ENTRY = 100

logger = logging.getLogger("raft")


def swap(src, dest, open_file=""):
    if os.path.exists(src):
        os.remove(src)
    os.rename(dest, src)
    if open_file:
        return open(src, open_file)
    return None


def write_json_line(f, data):
    f.write(json.dumps(data) + "\n")


def generate_election_timeout():
    return random.randint(100, 200) * 100


class Follower:
    def __init__(self, context):
        self.context = context
        self.election_timeout = generate_election_timeout()
        self.last_message_time = datetime.now()
        self.context.schedule(self.election_timeout, self.on_election_timeout)

    def on_election_timeout(self):
        elapsed_time = datetime.now() - self.last_message_time
        if elapsed_time.total_seconds() * 1000 >= self.election_timeout:
            self.context.as_candidate()
        else:
            self.context.schedule(self.election_timeout, self.on_election_timeout)

    def append_entries(self, req):
        self.last_message_time = datetime.now()
        if self.context.voted_for == req["leader_id"]:
            return self.context.__append_entries__(req)
        # TODO raft spec says don't respond,
        # instead responding false
        return {
            "success": False,
            "term": self.context.current_term,
        }

    def request_vote(self, req):
        self.last_message_time = datetime.now()
        return self.context.__request_vote__(req)

    def append_entry(self, req):
        return {"success": False, "redirect": self.context.voted_for}

    def results(self, query):
        return {"success": False, "redirect": self.context.voted_for}

    def snapshot(self):
        return {"success": False, "redirect": self.context.voted_for}


class Candidate:
    def __init__(self, context):
        self.context = context
        self.found_better_leader = False
        self.context.schedule(0, self.start_election)

    def start_election(self):
        logger.info(f"{self.context.name} started election")
        self.context.__vote__(self.context.name, self.context.current_term + 1)
        votes = 1
        for replica in self.context.replicas:
            if self.found_better_leader:
                return
            if replica == self.context.name:
                continue

            res = self.context.fetch(
                replica,
                "request_vote",
                {
                    "term": self.context.current_term,
                    "candidate_id": self.context.name,
                    "last_log_index": len(self.context.entries) - 1,
                    "last_log_term": self.context.entries[-1]["term"],
                    "snapshot_version": self.context.snapshot_version,
                },
            )

            if res and res["vote_granted"]:
                votes += 1
        logger.info(f"votes {votes}")
        if votes >= int(len(self.context.replicas) / 2) + 1:
            self.context.as_leader()
        elif not self.found_better_leader:
            self.election_timeout = generate_election_timeout()
            self.context.schedule(self.election_timeout, self.start_election)

    def append_entries(self, req):
        res = self.context.__append_entries__(req)
        if res["success"]:
            self.found_better_leader = True
            self.context.as_follower()
        return res

    def request_vote(self, req):
        res = self.context.__request_vote__(req)
        if res["vote_granted"]:
            self.found_better_leader = True
            self.context.as_follower()
        return res

    def append_entry(self, req):
        logger.info("candidate")
        return {"success": False}

    def results(self, query):
        return {"success": False}

    def snapshot(self):
        return {"success": False}


def sort_by_mayority(match_index_values):
    N = len(match_index_values)
    count = {}
    for s in match_index_values:
        for (k, _) in count.items():
            if k <= s:
                count[k] += 1
        if count.get(s) is None:
            count[s] = 1
    return list(
        map(
            lambda it: it[0],
            sorted(
                filter(lambda it: it[1] >= int(N / 2) + 1, count.items()),
                key=lambda it: it[1],
            ),
        )
    )


class Leader:
    def __init__(self, context):
        self.context = context
        self.heartbeat_timeout = HEARBEAT_TIMEOUT
        self.housekeeping_timeout = HOUSEKEEPING_TIMEOUT
        self.next_index = {
            replica: max(len(self.context.entries) - 1, 0) + 1
            for replica in self.context.replicas
        }
        self.snapshot_index = {
            replica: self.context.snapshot_version for replica in self.context.replicas
        }
        self.match_index = {replica: 0 for replica in self.context.replicas}
        self.context.schedule(self.heartbeat_timeout, self.heartbeat)
        self.context.schedule(self.housekeeping_timeout, self.housekeeping)
        # TODO commit blank to force replication sync

    def housekeeping(self):
        if self.context.voted_for == self.context.name:
            if self.context.commit_index >= HOUSEKEEPING_MAX_ENTRY:
                self.snapshot()
            self.context.schedule(self.housekeeping_timeout, self.housekeeping)

    def heartbeat(self):
        if self.context.voted_for == self.context.name:
            commited = self.update_replicas()
            if commited > self.context.commit_index:
                self.context.__update_commit_index__(commited)
            self.context.schedule(self.heartbeat_timeout, self.heartbeat)

    def append_entries(self, req):
        return self.context.__append_entries__(req)

    def request_vote(self, req):
        return self.context.__request_vote__(req)

    def update_replicas(self):
        for replica in self.context.replicas:
            if replica == self.context.name:
                self.match_index[replica] = len(self.context.entries)
                continue

            if self.snapshot_index[replica] != self.context.snapshot_version:
                res = self.context.fetch(
                    replica,
                    "append_entries",
                    {
                        "term": self.context.current_term,
                        "leader_id": self.context.name,
                        "snapshot": self.context.machine.snapshot(),
                        "snapshot_version": self.context.snapshot_version,
                    },
                )
                self.next_index[replica] = max(len(self.context.entries) - 1, 0) + 1
                self.match_index[replica] = 0
            else:
                prev_log_index = self.next_index[replica] - 1
                prev_log_term = self.context.entries[prev_log_index]["term"]
                res = self.context.fetch(
                    replica,
                    "append_entries",
                    {
                        "term": self.context.current_term,
                        "leader_id": self.context.name,
                        "prev_log_index": prev_log_index,
                        "prev_log_term": prev_log_term,
                        "entries": self.context.entries[(prev_log_index + 1) :],
                        "leader_commit": self.context.commit_index,
                        "snapshot_version": self.context.snapshot_version,
                    },
                )
            if res:
                if res["success"]:
                    self.next_index[replica] = len(self.context.entries)
                    self.match_index[replica] = len(self.context.entries) - 1
                elif res["snapshot_version"] < self.context.snapshot_version:
                    self.next_index[replica] = 1
                self.snapshot_index[replica] = res["snapshot_version"]
            else:
                self.next_index[replica] -= 1

        commited_sorted_by_mayority = sort_by_mayority(self.match_index.values())
        logger.info(f"commited: {commited_sorted_by_mayority}")
        for commited in commited_sorted_by_mayority:
            if commited <= self.context.commit_index:
                break
            if self.context.current_term == self.context.entries[commited]["term"]:
                # TODO concurrency over context.commit_index
                return commited
        return self.context.commit_index

    def append_entry(self, req):
        entry_index = len(self.context.entries)
        self.context.__append_entry__(
            entry_index,
            [
                {
                    "term": self.context.current_term,
                    "data": req,
                }
            ],
        )
        commited = self.update_replicas()
        if commited > self.context.commit_index:
            self.context.__update_commit_index__(commited)
        if commited == entry_index:
            return {
                "success": True,
                "id": entry_index,
            }
        return {
            "success": False,
            "id": entry_index,
        }

    def results(self, query):
        return {
            "success": True,
            "data": self.context.machine.results(query),
        }

    def snapshot(self):
        self.context.__snapshot__()
        return {"success": True}


class NopVM:
    def set_context(self, context):
        return

    def load_file(self, context, file_name):
        self.set_context(context)
        if os.path.exists(file_name):
            with open(file_name, "r+") as snapshot_file:
                snapshot_file.seek(0, 0)
                line = snapshot_file.readline()
                if line:
                    snapshot = json.loads(line)
                    self.reset(self, snapshot)

    def save_snapshot(self, file_name):
        with open(file_name, "w+") as f:
            write_json_line(f, self.snapshot())

    def reset(self, context, snapshot):
        return {}

    def snapshot(self):
        return {}

    def run(self, commands):
        return {}

    def results(self, query):
        return []


class Raft:
    def __fix_backups__(self, base_path):
        if os.path.exists(base_path + ".log.tmp"):
            if os.path.exists(base_path + ".conf.tmp"):
                os.remove(base_path + ".conf")
                os.rename(base_path + ".conf.tmp", base_path + ".conf")
            if os.path.exists(base_path + ".snapshot.tmp"):
                os.remove(base_path + ".snapshot")
                os.rename(base_path + ".snapshot.tmp", base_path + ".snapshot")
            os.remove(base_path + ".log")
            os.rename(base_path + ".log.tmp", base_path + ".log")
        else:
            if os.path.exists(base_path + ".conf.tmp"):
                os.remove(base_path + ".conf.tmp")
            if os.path.exists(base_path + ".snapshot.tmp"):
                os.remove(base_path + ".snapshot.tmp")

    def __init__(self, name, replicas, machine=NopVM(), base_path="/tmp/raft") -> None:
        self.__fix_backups__(base_path)

        self.name = name
        self.replicas = replicas
        self.machine = machine
        self.voted_for = None

        self.config = open(create_if_not_exists(base_path + ".conf"), "r+")
        self.config.seek(0, 0)
        line = self.config.readline()
        if line:
            conf = json.loads(line)
            self.commit_index = conf["commit_index"]
            self.last_applied = conf["last_applied"]
            self.snapshot_version = conf["snapshot_version"]
        else:
            self.snapshot_version = 0
            self.commit_index = 0
            self.last_applied = 0

        self.snapshot_file_name = base_path + ".snapshot"
        self.machine.load_file(self, self.snapshot_file_name)

        self.log = open(create_if_not_exists(base_path + ".log"), "r+")
        self.log.seek(0, 0)
        entries = []
        self.seek = []
        while True:
            seek = self.log.tell()
            line = self.log.readline()
            if not line:
                break
            self.seek.append(seek)
            entries.append(json.loads(line))
        if len(entries) > 0:
            self.current_term = self.entries[-1]["term"]
            self.entries = entries
            self.machine.run(self.entries[0 : self.commit_index + 1])
        else:
            self.entries = [{"term": 0, "data": None}]
            self.current_term = 0

        self.lock = RLock()
        self.scheduler = Scheduler()
        self.as_follower()

    def append_entry(self, req):
        return self.state.append_entry(req)

    def append_entries(self, req):
        return self.state.append_entries(req)

    def request_vote(self, req):
        return self.state.request_vote(req)

    def __append_entry__(self, start, reqs):
        with self.lock:
            logger.info(f"append_entry: {start}")
            if len(self.entries) < start:
                self.log.truncate(self.seek[start])
                self.entries = self.entries[:start]
                self.seek = self.seek[:start]
            self.entries += reqs
            for req in reqs:
                self.seek.append(self.log.tell())
                write_json_line(self.log, req)
            self.log.flush()

    def __vote__(self, voted_for, term):
        with self.lock:
            self.voted_for = voted_for
            self.current_term = term

    def save_config(self):
        self.config.seek(0, 0)
        write_json_line(
            self.config,
            {
                "commit_index": self.commit_index,
                "snapshot_version": self.snapshot_version,
                "last_index": self.last_applied,
            },
        )
        self.config.flush()

    def __append_entries__(self, req):
        if req["snapshot_version"] > self.snapshot_version:
            snapshot = req.get("snapshot")
            if snapshot is not None:
                self.machine.reset(self, snapshot)
                self.save_snapshot(req["snapshot_version"], [])
            return {
                "term": self.current_term,
                "success": False,
                "snapshot_version": self.snapshot_version,
            }
        elif req["snapshot_version"] < self.snapshot_version:
            return {
                "term": self.current_term,
                "success": False,
                "snapshot_version": self.snapshot_version,
            }
        if req["term"] < self.current_term:
            return {
                "term": self.current_term,
                "success": False,
                "snapshot_version": self.snapshot_version,
            }
        if req["prev_log_index"] >= len(self.entries):
            return {
                "term": self.current_term,
                "success": False,
                "snapshot_version": self.snapshot_version,
            }
        else:
            if self.entries[req["prev_log_index"]]["term"] != req["prev_log_term"]:
                return {
                    "term": self.current_term,
                    "success": False,
                    "snapshot_version": self.snapshot_version,
                }

        if len(req["entries"]) > 0:
            self.__append_entry__(req["prev_log_index"] + 1, req["entries"])

        if req["leader_commit"] > self.commit_index:
            self.__update_commit_index__(
                min(
                    req["leader_commit"],
                    len(self.entries) - 1,
                )
            )

        return {
            "term": self.current_term,
            "success": True,
            "snapshot_version": self.snapshot_version,
        }

    def __request_vote__(self, req):
        def upto_date(self, req):
            with self.lock:
                last_log_term = self.entries[-1]["term"]
                return (
                    last_log_term < req["last_log_term"]
                    or (
                        last_log_term == req["last_log_term"]
                        and len(self.entries) - 1 <= req["last_log_index"]
                    )
                ) and self.snapshot_version <= req["snapshot_version"]

        if req["term"] >= self.current_term:
            if (
                self.voted_for is None
                or self.voted_for == req["candidate_id"]
                or self.voted_for == self.name
            ):
                if upto_date(self, req):
                    self.__vote__(req["candidate_id"], req["term"])
                    return {
                        "term": self.current_term,
                        "vote_granted": True,
                        "snapshot_version": self.snapshot_version,
                    }
        return {
            "term": self.current_term,
            "vote_granted": False,
            "snapshot_version": self.snapshot_version,
        }

    def __update_commit_index__(self, commited):
        prev_index = self.commit_index
        self.commit_index = commited
        self.machine.run(self.entries[prev_index + 1 : commited + 1])
        self.save_config()

    def __snapshot__(self):
        snapshot_version = self.snapshot_version + self.commit_index
        entries = self.entries[self.commit_index + 1 :]
        self.save_snapshot(snapshot_version, entries)

    def save_snapshot(self, snapshot_version, entries):
        self.machine.save_snapshot(self.snapshot_file_name + ".tmp")

        with open(self.config.name + ".tmp", "w+") as f:
            write_json_line(
                f,
                {
                    "snapshot_version": snapshot_version,
                    "commit_index": 0,
                    "last_applied": 0,
                },
            )

        seek = []
        with open(self.log.name + ".tmp", "w+") as f:
            for e in entries:
                seek.append(f.tell())
                write_json_line(f, e)

        with self.lock:
            swap(
                self.snapshot_file_name,
                self.snapshot_file_name + ".tmp",
            )
            self.config = swap(self.config.name, self.config.name + ".tmp", "r+")
            self.log = swap(self.log.name, self.log.name + ".tmp", "r+")

            self.seek = seek
            self.entries = entries
            self.snapshot_version = snapshot_version
            self.entries = [{"term": self.current_term, "data": None}] + entries
            self.commit_index = 0
            self.last_applied = 0

    def as_candidate(self):
        with self.lock:
            logger.info(f"{self.name} as candidate")
            self.state = None
            self.state = Candidate(self)

    def as_leader(self):
        with self.lock:
            logger.info(f"{self.name} as leader")
            self.state = None
            self.state = Leader(self)

    def as_follower(self):
        with self.lock:
            logger.info(f"{self.name} as follower")
            self.state = None
            self.state = Follower(self)

    def fetch(self, replica, service, data):
        try:
            response = requests.post(f"http://{replica}/{service}", json=data)
            if response.status_code == 200:
                return response.json()
            logger.info(f"{response.status_code}, {response.text}")
        except Exception:
            logger.exception(f"Calling {replica}/{service}")
        return None

    def schedule(self, delaymillis, func):
        # logger.info(self.name, " schedule ", delaymillis)
        self.scheduler.schedule(
            timedelta(milliseconds=delaymillis),
            func,
        )

    def results(self, query):
        return self.state.results(query)

    def snapshot(self):
        return self.state.snapshot()


from flask import Flask, request
import os


def add_raft_routes(app, raft: Raft):
    @app.route("/request_vote", methods=["POST"])
    def request_vote():
        data = request.get_json()
        return raft.request_vote(data)

    @app.route("/append_entries", methods=["POST"])
    def append_entries():
        data = request.get_json()
        return raft.append_entries(data)

    @app.route("/append_entry", methods=["POST"])
    def append_entry():
        data = request.get_json()
        return raft.append_entry(data)

    @app.route("/show")
    def show():
        res = {
            "entries": raft.entries,
            "voted_for": raft.voted_for,
            "current_term": raft.current_term,
            "commit_index": raft.commit_index,
            "replicas": raft.replicas,
            "name": raft.name,
            "snapshot_version": raft.snapshot_version,
            "state": raft.state.__class__.__name__,
        }
        return res

    @app.route("/snapshot")
    def snapshot():
        raft.snapshot()
        return {}

    @app.route("/database/<key>", methods=["DELETE", "GET"])
    def results(key):
        if request.method == "DELETE":
            return raft.append_entry(
                {
                    "op": "-",
                    "key": key,
                }
            )
        else:
            return raft.results(key)

    @app.route("/database/<key>", methods=["PUT"])
    def save(key):
        return raft.append_entry(
            {
                "op": "+",
                "key": key,
                "val": request.get_json(),
            }
        )

    return raft


class KeyValueVM(NopVM):
    data = {}

    def reset(self, context, snapshot):
        self.data = snapshot

    def snapshot(self):
        return self.data

    def run(self, commands):
        print(commands)
        for command in commands:
            command = command["data"]
            key = command["key"]
            val = command.get("val")
            op = command.get("op", "put")
            if op == "+":
                self.data[key] = val
            elif op == "-":
                self.data.pop(key, None)

        return None

    def results(self, query):
        return self.data.get(query)


def test():
    response = requests.post("http://localhost:8083/append_entry", json={"a": "a"})
    for i in range(10, 15):
        requests.put(f"http://localhost:8082/database/{i}", json={"index": i})
    response = requests.get("http://localhost:8083/database/1")


if __name__ == "__main__":
    app = Flask(__name__)
    logging.basicConfig(level=logging.DEBUG)
    raft = Raft(
        os.environ["NAME"],
        os.environ["REPLICAS"].split(","),
        KeyValueVM(),
    )
    add_raft_routes(app, raft)
    app.run(host="0.0.0.0", port=80, threaded=True)