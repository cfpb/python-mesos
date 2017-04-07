"""Get metrics from mesos machines and index them in elasticsearch

Usage: [PYTHON_MESOS_LOG_LEVEL] [PYTHON_MESOS_USERNAME=<mesos_username>] [PYTHON_MESOS_PASSWORD=<mesos_password>] [PYTHON_MESOS_ES_<key>=<val>] python monitor_mesos.py <config_file>

Environment Variables:
    PYTHON_MESOS_LOG_LEVEL: python logging level to use (i.e. ERROR, INFO, DEBUG)
    PYTHON_MESOS_USERNAME: username for authenticating to the mesos API (used for all machines)
    PYTHON_MESOS_PASSWORD: password for authenticating to the mesos API (used for all machines)
    PYTHON_MESOS_ES_<key>: override for key/value pair in config["elasticsearch"]
        For instance, PYTHON_MESOS_ES_password will override config["elasticsearch"]["password"]
        See the section below for a list of possible keys.

Config file is a json file with the following structure:
    elasticsearch: object representing where to index the data
        url: base url for elasticsearch
        index: name of the ES index
        rectype: ES rectype to use for the records
        username: username to log in to ES
        password: password to log in to ES
    machines: *array* of mesos machines to query (masters and agents)
        name: what to call the machine
        type: "master" or "agent"
        url: base url for mesos on the machine
        username: mesos username for pulling stats (optional)
        password: mesos password for pulling stats (optional)
"""
from __future__ import print_function
import datetime as dt
import json
import logging
import os
import sys

import requests


def strip_keys(data, keys):
    """return a copy of dict `data` with certain keys missing"""
    return dict([(key, val) for key, val in data.items() if key not in keys])


def index_rec(rec, es_config):
    """Index a record in elasticsearch
    :param es_config: Dict with url/index/rectype and optionally username/password
    :param rec: A dict to be indexed in ES"""
    rectype = es_config["rectype" if rec["type"] == "mesos_snapshot" else "container_rectype"]
    url = "{url}/{index}/{es_rectype}".format(es_rectype=rectype, **es_config)

    args = {}
    if "username" in es_config:
        args["auth"] = (es_config["username"], es_config["password"])

    result = requests.post(url, data=json.dumps(rec), verify=False, **args)
    result.raise_for_status()
    return result


def make_metrics_record(machine, metrics, timestamp):
    """Make the full record to be indexed
    :param machine: the machine's config entry
    :param metrics: the metrics output received from mesos
    :param timestamp: string to use as a timestamp for the record"""
    return {
        "type": "mesos_snapshot",
        "@timestamp": timestamp,
        "host": machine["name"],
        "tags": ["mesos", machine["type"]],
        "message": {
            "machine": strip_keys(machine, ["username", "password"]),
            "metrics": metrics,
        },
    }


def get_machine_metrics(machine_url, auth=None):
    """Get the metrics for a mesos machine
    :param machine_url: base url for a mesos instance"""
    logging.info("Getting metrics for machine %s", machine_url)
    logging.debug("Using auth %r", auth)
    result = requests.get(machine_url + "/metrics/snapshot", auth=auth, verify=False)
    result.raise_for_status()
    return result.json()


def make_stats_record(machine, container_stat, timestamp):
    """Make the full record to be indexed
    :param machine: the machine's config entry
    :param metrics: the metrics output received from mesos
    :param timestamp: string to use as a timestamp for the record"""
    # Calculate mem_free_bytes
    stats = container_stat.get("statistics")
    if stats:
        total, limit = stats.get("mem_total_bytes"), stats.get("mem_limit_bytes")
        if total and limit:
            stats["mem_free_bytes"] = limit - total

    return {
        "type": "mesos_container",
        "@timestamp": timestamp,
        "host": machine["name"],
        "tags": ["mesos", machine["type"]],
        "message": {
            "machine": strip_keys(machine, ["username", "password"]),
            "container": container_stat,
        },
    }


def get_statistics(machine_url, auth=None):
    """Get the statistics for a mesos agent
    :param machine_url: base url for a mesos instance"""
    logging.info("Getting statistics for machine %s", machine_url)
    logging.debug("Using auth %r", auth)
    result = requests.get(machine_url + "/monitor/statistics.json", auth=auth, verify=False)
    result.raise_for_status()
    return result.json()


def index_machines(config):
    """Get metrics for all the machines listed in the config and index them to elasticsearch"""
    timestamp = dt.datetime.utcnow().isoformat()
    for machine in config["machines"]:
        auth = None
        if "username" in machine:
            auth = (machine["username"], machine["password"])
        metrics = get_machine_metrics(machine["url"], auth=auth)
        record = make_metrics_record(machine, metrics, timestamp)
        logging.debug("Ready to index record %r", record)
        index_rec(record, config["elasticsearch"])
        if machine["type"] == "agent":
            container_stats = get_statistics(machine["url"], auth=auth)
            for stat in container_stats:
                record = make_stats_record(machine, stat, timestamp)
                logging.debug("Ready to index container record %r", record)
                index_rec(record, config["elasticsearch"])


def main():
    """Load the config file, get metrics for all machines, and index results in ES"""
    logging.basicConfig(level=os.environ.get("PYTHON_MESOS_LOG_LEVEL", "ERROR"))
    with open(sys.argv[1]) as config_file:
        config = json.load(config_file)
    logging.info("Loaded config")
    logging.debug(repr(config))
    config["elasticsearch"].update(
        dict([
            (key[len("PYTHON_MESOS_ES_"):], val) for key, val in os.environ.items()
            if key.startswith("PYTHON_MESOS_ES_")
        ])
    )
    # Set Mesos username/password for all machines
    for var, key in [("PYTHON_MESOS_USERNAME", "username"), ("PYTHON_MESOS_PASSWORD", "password")]:
        val = os.environ.get(var)
        if val:
            for machine in config["machines"]:
                machine[key] = val

    logging.info("Updated config with environment variables")
    logging.debug(repr(config))
    index_machines(config)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ["help", "--help", "-h"]:
        print(__doc__)
    else:
        main()
