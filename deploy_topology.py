#!/usr/bin/python3

"""
This script creates a GNS3 project, adds nodes, interconnect and boots all of
them, then applies Day-0 configuration to them. It also creates an Ansible
inventory that can be used for further configuration.
"""

import argparse
import atexit
import logging as log
import pycdlib
import shutil
import sys
import tempfile

from json import dumps
from subprocess import call
from time import sleep
from re import sub
from requests import get, post, put, delete
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

    for node_name in CONFIG["nodes"]:
        node_config = CONFIG["nodes"][node_name]
        try:
            node_config["template_id"] = templates[node_config["template_name"]]
        except KeyError:
            log.error(
                "No template '%s' found on server.",
                node_config["template_name"]
            )
            exit(1)

def create_cloud_config(node_name):
    """
    Create cloud config files, i.e. the cloud-init configuration and an iso
    image containing those files.
    """
    log.debug("Creating cloud config for %s", node_name)
    tmpdir = tempfile.mkdtemp()
    log.debug("Created temporary directory '%s' for %s", tmpdir, node_name)
    fn = f"{tmpdir}/network-config"
    with open(fn, "w") as conffile:
        config = (
            "network:\n"
            "  version: 2\n"
            "  ethernets:\n"
            "    ens3:\n"
            "      dhcp4: false\n"
            "      dhcp6: false\n"
            "      addresses:\n"
           f"        - \"{CONFIG["nodes"][node_name]['ip']}\"\n"
            "      routes:\n"
            "        - to: default\n"
           f"          via: \"{CONFIG["nodes"][node_name]['gw']}\"\n"
            "      nameservers:\n"
            "        addresses:\n"
            "          - 9.9.9.9\n"
            "          - 1.1.1.1\n"
        )
        conffile.write(config)

    iso_name = f"{tmpdir}/{node_name}.iso"
    log.debug("Create cloud-init iso for %s: '%s'", node_name)
    iso = pycdlib.PyCdlib()
    iso.new(vol_ident='cidata', udf='2.60')
    iso.add_file(fn, udf_path="/network-config")
    iso.write(iso_name)
    iso.close()

    # ensure the stuff gets deleted when the program exits
    atexit.register(shutil.rmtree, tmpdir)

    return iso_name

def add_nodes():
    """
    This function adds the defined nodes to the project.
    """

    # Add each node individually
    for node_name in CONFIG["nodes"]:
        node_config = CONFIG["nodes"][node_name]

        log.debug(
            "Node configuration for \"%s\": \"%s\"",
            node_name,
            node_config
        )

        url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/templates/{node_config["template_id"]}"
        data = {
            "compute_id": "local",
            "name": node_name,
            "x": node_config["x"],
            "y": node_config["y"]
        }

        log.debug("Creating node %s with data: \"%s\"", node_name, data)
        # Create the node
        response = post(url, data=dumps(data))
        log.debug("Response: \"%s\"", response.json())

        if response.status_code != 201:
            log.error(
                "Received HTTP error %d when adding node %s: \"%s\"",
                response.status_code,
                node_name,
                response.json()["message"]
            )
            exit(1)

        # Update node configuration with returned details
        instance_data = response.json()
        node_config["console"] = instance_data["console"]
        node_config["node_id"] = instance_data["node_id"]
        node_config["ports"] = instance_data["ports"]

        if 'cloud_init' in node_config:
            iso_name = create_cloud_config(node_name)
            data["properties"] = {
                "cdrom_image": iso_name
            }

            log.debug("Uploading cloud-init ISO image for node %s: ", node_name)

            url = f"{CONFIG["gns3_server_url"]}/v2/projects/{CONFIG["project_id"]}/nodes/{node_config["node_id"]}"
            response = put(url, data=dumps(data))

            if response.status_code != 200:
                log.error(
                    "Received HTTP error %d when uploading cloud-init image for node %s: \"%s\"",
                    response.status_code,
                    node_name,
                    response.json()["message"]
                )
                exit(1)

        log.debug(
            "Updated node configuration for \"%s\": \"%s\"",
            node_name,
            node_config
        )


def add_links():
    """
    Create links between the nodes
    """

    for link in CONFIG["links"]:

        # Add port details to links
        for member in link:
            log.debug(CONFIG["nodes"])
            log.debug("Member name: %s", member["name"])


            try:
                node = CONFIG["nodes"][member["name"]]
            except KeyError:
                log.error(
                    "Node \"%s\" defined in link \"%s\"does not exist.",
                    member["name"],
                    link
                )
                exit(1)

            member["node_id"] = node["node_id"]
            member["adapter_number"] = node["ports"][member["interface"]]["adapter_number"]
            member["port_number"] = node["ports"][member["interface"]]["port_number"]

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

    for node_name, config in CONFIG["nodes"].items():
        if "cmdfile" in config:
            expect_cmd = [
                "expect",
                f"day0-{config["cmdfile"]}.exp",
                gns3_server,
                str(config["console"]),
                node_name,
                config["ip"],
                config["gw"],
                ">/dev/null"
            ]
            call(expect_cmd)


def build_ansible_hosts(fh):
    """
    Creating an Ansible hosts file from the nodes
    """

    with fh as hosts_file:
        ansible_groups = {}

        for node, config in CONFIG["nodes"].items():
            if not "ip" in config:
                continue

            # Writing the hostname and its IP address to the inventory
            # file. The sub function removes the /xx or
            # "xxx.xxx.xxx.xxx" portion of the address.
            hosts_file.write(
                f"{node} ansible_host={sub("/.*$| .*$", "", config["ip"])}\n"
            )

            if not "groups" in config:
                continue

            for group in config["groups"]:
                if group not in ansible_groups:
                    ansible_groups[group] = [node]
                else:
                    ansible_groups[group].append(node)

            log.debug("Gathered groups: %s", ansible_groups)

        # Create inventory groups
        for group, hosts in ansible_groups.items():
            hosts_file.write("\n")
            hosts_file.write(f"[{group}]\n")
            for host in hosts:
                hosts_file.write(f"{host}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="""Create a topology on GNS3.""")
    parser.add_argument(
        "config_file",
        type=argparse.FileType('r', encoding='UTF-8'),
        help="File path of the configuration file",
    )

    parser.add_argument(
        "-s",
        dest='gns3_server_url',
        help='GNS3 server URL (e.g. http://10.0.0.10:3080/)',
        required=False
    )

    parser.add_argument(
        "--project-name",
        help='GNS3 project name',
        required=False
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

    # FIXME: ensure shutils.rmtree doesn't kill the working directory
    import os
    os.chdir("/tmp/run_gns3a")

    # overwrite some definitions from the configuration file
    if args.gns3_server_url:
        CONFIG["gns3_server_url"] = args.gns3_server_url

    if args.project_name:
        CONFIG["project_name"] = args.project_name

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

    # Dump final config
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
