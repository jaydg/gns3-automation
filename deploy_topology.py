#!/usr/bin/python3

"""
This script creates a GNS3 project, adds nodes, interconnect and boots all of
them, then applies Day-0 configuration to them. It also creates an Ansible
inventory that can be used for further configuration.
"""

import argparse
import logging as log
import sys

from json import dumps
from subprocess import call
from time import sleep
from re import sub
from requests import get, post, delete
from urllib.parse import urlparse
from yaml import load, safe_dump
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


def create_project(name):
    """
    Checking if a project with a given name already exists; if yes, deleting it.
    Then the function (re)creates the project and returns the project ID.
    """

    # Finding the project ID if a project with the given name exists.
    url = f"{CONFIG["gns3_server_url"]}/v2/projects"
    response = get(url)
    if response.status_code == 200:
        body = response.json()
        project = next((item for item in body if item["name"] == CONFIG["project_name"]), None)
    else:
        log.error(
            "Received HTTP error %d when checking if the project already exists! Exiting.",
            response.status_code
        )
        exit(1)

    # Deleting the project if it already exists.
    if project is not None:
        delete_project_id = project["project_id"]
        url = f"{CONFIG["gns3_server_url"]}/v2/projects/{delete_project_id}"

        response = delete(url)
        if response.status_code != 204:
            log.error(
                "Received HTTP error %d when deleting the existing project! Exiting.",
                response.status_code
            )
            exit(1)

    # (Re)creating the project
    url = f"{CONFIG["gns3_server_url"]}/v2/projects"
    data = {
        "name": name
    }

    response = post(url, data=dumps(data))
    if response.status_code == 201:
        body = response.json()
        # Adding the project ID to the config
        CONFIG["project_id"] = body["project_id"]
    else:
        log.error(
            "Received HTTP error %d when creating the project! Exiting.",
            response.status_code
        )
        exit(1)


def assign_template_ids():
    """
    Retrieve template information and assign the template IDs to the node
    definitions. The template ID is required when creating nodes from templates.
    """

    url = f"{CONFIG["gns3_server_url"]}/v2/templates"
    response = get(url)

    templates = {}

    if response.status_code != 200:
        log.error(
            "Received HTTP error %d when retrieving templates! Exiting.",
            response.status_code
        )
        exit(1)

    body = response.json()
    templates = { t["name"]: t["template_id"] for t in body }

    for template in CONFIG["nodes"]:
        try:
            template["template_id"] = templates[template["template_name"]]
        except KeyError:
            log.error(
                "No template '%s' found on server.",
                template["template_name"]
            )
            exit(1)


def add_nodes():
    """
    This function adds a node to the project already created.
    """

    # Adding nodes
    for template in CONFIG["nodes"]:
        instance_seq = 1
        for instance in template["instances"]:
            # Adding node name to the config
            instance["name"] = template["template_name"].replace(" ", "") + \
                               "-"  + str(instance_seq)

            # Creating the node
            url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/templates/{template["template_id"]}"

            data = {
                "compute_id": "local",
                "name": instance["name"],
                "x": instance["x"],
                "y": instance["y"]
            }

            response = post(url, data=dumps(data))
            if response.status_code == 201:
                instance_seq += 1
            else:
                log.error(
                    "Received HTTP error %d when adding node %s! Exiting.",
                    response.status_code,
                    instance["name"]
                )
                exit(1)

    # Retrieving all nodes in the project, the assigning node IDs and console
    # port numbers by searching the node's name, then appending the config with
    # them.
    url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/nodes"

    response = get(url)

    if response.status_code == 200:
        body = response.json()
        for template in CONFIG["nodes"]:
            for instance in template["instances"]:
                instance["node_id"] = next((item["node_id"] \
                                    for item in body if item["name"] == instance["name"]), None)
                instance["console"] = next((item["console"] \
                                    for item in body if item["name"] == instance["name"]), None)
    else:
        log.error(
            "Received HTTP error %d when retrieving nodes! Exiting.",
            response.status_code
        )
        exit(1)


def add_links():
    """
    Creating links between the nodes and their interfaces defined in the config
    """

    for link in CONFIG["links"]:
        for member in link:
            for template in CONFIG["nodes"]:
                for instance in template["instances"]:
                    if member["name"] == instance["name"]:
                        member["node_id"] = instance["node_id"]

                        url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/nodes/{member["node_id"]}"

                        response = get(url)
                        body = response.json()

                        member["adapter_number"] = body["ports"][member["interface"]]["adapter_number"]
                        member["port_number"] = body["ports"][member["interface"]]["port_number"]

        url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/links"

        data = {
            "nodes": [
               {
                   "node_id": link[0]["node_id"],
                   "adapter_number": link[0]["adapter_number"],
                   "port_number": link[0]["port_number"]
               },
               {
                   "node_id": link[1]["node_id"],
                   "adapter_number": link[1]["adapter_number"],
                   "port_number": link[1]["port_number"]
               }
            ]
        }

        response = post(url, data=dumps(data))
        if response.status_code != 201:
            log.error(
                "Error %d when creating link %s adapter %s port %s -- %s adapter %s port %s",
                response.status_code,
                link[0]["node_id"], link[0]["adapter_number"], link[0]["port_number"],
                link[1]["node_id"], link[1]["adapter_number"], link[1]["port_number"]
            )
            exit(1)


def start_nodes():
    """
    Booting all nodes in the topology.
    """
    url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/nodes/start"

    response = post(url)
    if response.status_code == 204:
        # Wait 10s for nodes to start booting
        sleep(10)
    else:
        log.error(
            "Received HTTP error %d when starting nodes! Exiting.",
            response.status_code
        )
        exit(1)


def day0_config():
    """
    Deploying Day-0 configuration
    """

    gns3_server, _ = urlparse(CONFIG["gns3_server_url"]).netloc.split(':')

    for template in CONFIG["nodes"]:
        if template["os"] != "none":
            for instance in template["instances"]:
                expect_cmd = ["expect", "day0-%s.exp" % template["os"], gns3_server, \
                              str(instance["console"]), template["os"] + \
                              str(template["instances"].index(instance) + 1), \
                              instance["ip"], instance["gw"], ">/dev/null"]
                call(expect_cmd)


def build_ansible_hosts(fh):
    """
    Creating an Ansible hosts file from the nodes
    """

    with fh as hosts_file:
        for template in CONFIG["nodes"]:
            if template["os"] != "none":
                # Creating inventory groups based on OS
                hosts_file.write("[%s]\n" % template["os"])
                for instance in template["instances"]:
                    # Writing the hostname and its IP address to the inventory
                    # file. The sub function reremoves the /xx or
                    # "xxx.xxx.xxx.xxx" portion of the address.
                    hosts_file.write(
                        "%s ansible_host=%s\n" % \
                        (instance["name"], sub("/.*$| .*$", "", instance["ip"]))
                    )
                hosts_file.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="""Create a topology on GNS3.""")
    parser.add_argument(
        "config_file",
        type=argparse.FileType('r', encoding='UTF-8'),
        help="File path of the configuration file",
    )

    parser.add_argument(
        "--ansible-hosts",
        type=argparse.FileType('w'),
        help='Create Ansible hosts file for the topology',
        required=False
    )

    parser.add_argument(
        "--output-file",
        type=argparse.FileType('w'),
        help='Save final topology config into file',
        required=False
    )

    parser.add_argument(
        "-d",
        dest='debug',
        action='store_true',
        default=False,
        help="Enable debug logging",
    )

    args = parser.parse_args()

    FORMAT = '%(asctime)s %(levelname)s: %(message)s'
    if args.debug:
        log.basicConfig(stream=sys.stderr, level=log.DEBUG, format=FORMAT)
    else:
        log.basicConfig(stream=sys.stderr, level=log.INFO, format=FORMAT)

    # Loading config file
    with args.config_file as config_file:
        CONFIG = load(config_file, Loader=Loader)

    # Create project and add its ID to the config
    log.info("Creating GNS3 project")
    create_project(CONFIG["project_name"])

    # Add template IDs to the config
    log.info("Retrieving template IDs")
    assign_template_ids()

    # Add nodes to the topology
    log.info("Adding nodes")
    add_nodes()

    # Create links between the nodes
    log.info("Adding links")
    add_links()

    # Creating inventory file for Ansible
    if args.ansible_hosts:
        log.info("Generating Ansible inventory file")
        build_ansible_hosts(args.ansible_hosts)

    # Dump final config into "topology_full.yml"
    if args.output_file:
        log.info("Saving final topology config.")
        with args.output_file as topology_file:
            safe_dump(CONFIG, topology_file, default_flow_style=False)

    # Start nodes
    log.info("Starting nodes")
    start_nodes()

    # Day-0 configuration
    log.info("Applying Day-0 configuration")
    day0_config()
