import requests
import os
import time
import subprocess
from health_server import *

import logging

LOG = logging.getLogger("WatchdogSideCar")
LOG.setLevel(logging.ERROR)


def revive(ip):
    LOG.info("Process " + ip + " has died. Starting it up again...")
    result = subprocess.run(
        ["docker", "start", ip],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    LOG.info(
        "Process "
        + ip
        + " is up again. Result={}. Output={}. Error={}".format(
            result.returncode, result.stdout, result.stderr
        )
    )


def run_election():
    candidates = {}
    for ip in get_watchdogs_ips():
        try:
            response = requests.get("http://" + ip + ":80" + "/id")
            id = response.json().get("id")
            LOG.info(
                "Process "
                + ip
                + " says its id is "
                + id
                + " and its "
                + str(response.json().get("leader"))
                + " that it is the leader"
            )
            if response.json().get("leader"):
                return ip, id
            candidates[ip] = id
        except:
            LOG.info("Process " + ip + " did not answered")
            pass
    leaderIp = max(candidates, key=candidates.get)
    leaderId = candidates[leaderIp]
    LOG.info("Now " + leaderIp + " is leader")
    return leaderIp, leaderId


def i_am_leader(leaderId):
    return leaderId == os.environ["HOSTNAME"]


def get_watchdogs_ips():
    ips = []
    for i in range(int(os.environ["N_REPLICAS"])):
        ips.append(os.environ["IP_PREFIX"] + "_watchdog_" + str(i + 1))
    # LOG.info('WATCHDOG IPS: ', ips)
    return ips


def main():
    ips = []
    for processKey in [
        "ROUTER",
        "STARS5",
        "COMMENT",
        "BUSSINESS",
        "USERS",
        "HISTOGRAM",
        "FUNNY",
        "STARS5_MAPPER",
        "COMMENT_MAPPER",
        "HISTOGRAM_MAPPER",
        "FUNNY_MAPPER",
        "KEVASTO",
    ]:
        nReplicas = int(os.environ.get("N_" + processKey, 1))
        processIp = os.environ["IP_" + processKey]
        for i in range(nReplicas):
            ip = os.environ["IP_PREFIX"] + "_" + processIp + "_" + str(i + 1)
            ips.append(ip)
    iAmLeader = [False]
    leaderServer = LeaderServer(iAmLeader)
    time.sleep(10)  # Le doy tiempo a los procesos para levantar el flask
    leaderIp, leaderId = run_election()
    workersIps = [ip for ip in get_watchdogs_ips() if ip != leaderIp]
    while True:
        time.sleep(3)
        iAmLeader[0] = i_am_leader(leaderId)
        if iAmLeader[0]:
            LOG.info("[LEADER] Checking processes health...")
            for ip in ips + workersIps:
                # LOG.info('http://' + ip + ':' + port + '/health')
                try:
                    requests.get("http://" + ip + ":80/health")
                except:
                    revive(ip)
        else:
            LOG.info("[WORKER] Checking leader health...")
            try:
                requests.get("http://" + leaderIp + ":80/health")
                # LOG.info('http://' + leaderIp + ':' + port + '/health')
            except:
                LOG.info("Leader has died. Running leader election")
                leaderIp, leaderId = run_election()
                workersIps = [ip for ip in get_watchdogs_ips() if ip != leaderIp]


if __name__ == "__main__":
    main()
