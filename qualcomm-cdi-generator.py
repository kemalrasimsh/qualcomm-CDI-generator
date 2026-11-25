#!/usr/bin/env python3

# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#
# SPDX-License-Identifier: BSD-3-Clause-Clear

# This tool generates Container Device Interface (CDI) specification files as
# defined by the CNCF Container Device Interface project:
#
#   https://github.com/cncf-tags/container-device-interface
#
# The generated JSON files follow the CDI spec format (see the CDI_VERSION
# below) and can be validated with the upstream 'cdi' tool from that project.

# Allow PEP 585/604 annotations (list[str], int | None) on Python 3.8/3.9.
from __future__ import annotations

import fcntl
import fnmatch
import glob
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import logging
import argparse

# PyYAML. The hexagon-dsp board configuration is YAML and is read with it (see
# parse_dsp_conf()), which is the one thing this script needs from outside the
# standard library: an image installing that configuration installs a parser
# for it too, and a host with neither resolves its firmware the other way.
import yaml

KNOWN_CLASSES = (
    'gpu',
    'v4l2',
    'camera',
    'audio',
    'display',
    'dmaheap',
    'fastrpc-cdsp',
    'fastrpc-adsp',
    'fastrpc-gdsp',
)

# CDI specification version emitted in every generated spec file. See the CNCF
# Container Device Interface project for the format definition:
# https://github.com/cncf-tags/container-device-interface
#
# 0.7.0 is the minimum version that defines containerEdits.additionalGids, which
# the generated specs rely on to grant access to the group-owned device nodes.
# Emitting it under an older version makes the spec fail validation.
CDI_VERSION = "0.7.0"

# CDI vendor namespaces a generated 'kind' is prefixed with, and the classes
# that are not in the Qualcomm one.
#
# A kind names the vendor whose interface the class is, and three of these are
# nobody's in particular: v4l2 is the kernel's own video interface, and the
# audio and display classes hand over a PulseAudio socket and a Wayland
# socket. Nothing about the three is Qualcomm specific -- the same spec
# describes the same resources on any Linux host running those servers -- so
# they are emitted as linux.org/<class> and the hardware classes as
# qualcomm.com/<class>.
#
# A spec is named on disk after the vendor it is in as well, so the file
# holding a kind says which namespace it came from: linux.org/audio is written
# to linux-audio.json beside the qualcomm.com/gpu in qualcomm-gpu.json (see
# class_vendor() and class_spec_filename()).
CDI_VENDOR = "qualcomm.com"
LINUX_VENDOR = "linux.org"
CLASS_VENDOR = {
    'v4l2':    LINUX_VENDOR,
    'audio':   LINUX_VENDOR,
    'display': LINUX_VENDOR,
}

# Indentation of the generated JSON. The specs are read and diffed by hand when
# a target does not expose what was expected, so they are written formatted
# rather than as one long line. Runtimes parse either form identically.
JSON_INDENT = 2

# The "this file was generated" note carried at the top of every spec.
#
# JSON has no comment syntax, so the note rides in the spec's own 'annotations'
# map, which build_cdi_spec() emits between 'kind' and 'devices' -- the top of
# the file, where the comment would have gone, and the order the CDI Spec struct
# declares. CDI defines the field as specific to itself and not affecting
# container metadata, so the note describes the spec to whoever reads it and
# never reaches the workload.
#
# Only a field the Spec struct declares may be emitted: the runtimes unmarshal
# these files with unknown fields disallowed and reject the whole spec on the
# first key they do not recognise, which costs the container every device in the
# class.
#
# The keys are qualified with the vendor of the spec they sit on, so they read
# like the 'kind' next to them and cannot collide with another tool's metadata
# on the same spec.
#
# No timestamp or hostname is recorded: two runs that probe the same devices
# should produce byte-identical files, so that a diff after an upgrade or a
# hardware change shows only what actually changed.
SPEC_ANNOTATIONS = {
    'generated-by': "qualcomm-cdi-generator",
    'generated-note': "This file is generated from the devices discovered "
                      "on this host. Any manual changes will be overwritten, "
                      "as the file is regenerated from scratch on every "
                      "system boot by the qualcomm-cdi-generator.service",
}

# Host sockets bind-mounted into the container, per device class. These classes
# are not reached through a device node at all: the container talks to a server
# on the host over a unix socket, so the socket is the whole resource and the
# spec carries a mount instead of a deviceNodes entry.
#
# Each class lists one or more glob patterns, tried in order by find_sockets()
# and stopping at the first that matches: a server reachable system-wide under
# /run is preferred, and the per-user runtime directory is the fallback for the
# usual session case. The patterns cover both layouts a Qualcomm target can
# have:
#   * PulseAudio accepts clients on /run/pulse/native when it runs system-wide,
#     otherwise on the per-user runtime socket,
#   * the camera server (qmmf/CamX) listens on /run/cam_server,
#   * the Wayland compositor exposes its socket in /run for a system compositor,
#     otherwise in the user runtime dir.
# The user runtime directory is globbed rather than pinned to uid 1000, so a
# target running these servers under a different user is still picked up.
#
# Inside the container they are all presented under /run, the system-wide
# location a client is pointed at (WAYLAND_DISPLAY=wayland-0 resolved against
# XDG_RUNTIME_DIR=/run, PULSE_SERVER=unix:/run/pulse/native); a socket the
# host keeps in a per-user runtime directory is normalised up to it (see
# SOCKET_CONTAINER_DIRS).
#
# A class whose socket is absent is skipped, exactly as a class whose device
# nodes are absent is: the server is not running, so the spec would hand the
# container a path that cannot be connected to.
CLASS_SOCKET_GROUPS = {
    "audio":   (
        ("/run/pulse/native", "/run/user/*/pulse/native"),
        ("/run/pipewire/pipewire-*", "/run/user/*/pipewire-*"),
    ),
    "camera":   (
        ("/run/cam_server/le_camera_socket",),
    ),
    "display":   (
        ("/run/wayland-*", "/run/user/*/wayland-*"),
    ),
}

# Suffixes a class's socket glob can match that must not be mounted. A Wayland
# compositor keeps a '<socket>.lock' lock file next to every socket it serves,
# so '/run/user/*/wayland-*' matches /run/user/1000/wayland-1 and
# /run/user/1000/wayland-1.lock alike, and PipeWire serves a '<socket>-manager'
# socket beside pipewire-0 that is a session manager's interface rather than
# where a client connects.
#
# Mounting the lock file would hand the container a path no client connects to,
# and give it the host compositor's lock to take. These are dropped before a
# pattern counts as matched, so a runtime directory holding nothing but a
# leftover lock file falls through to the next pattern instead of being taken
# for a running compositor.
SOCKET_IGNORE_SUFFIXES = ('.lock','-manager',)

# Mount options for the sockets above. Unlike the read-only devicetree model
# node these are read-write: a unix socket is useless to a client that cannot
# write to it.
SOCKET_MOUNT_OPTIONS = ("nosuid", "noexec", "rw", "bind")

# Directory the DSP firmware files appear under inside the container.
#
# This is where they have to appear and the only place they appear: the host
# directory they came from is board specific and is not mounted, so a file
# keeps its per-DSP sub-path and nothing else of the path it was found at --
# <board dir>/cdsp/fastrpc_shell_3 is handed over as
# /usr/lib/dsp/cdsp/fastrpc_shell_3.
DSP_FIRMWARE_CONTAINER_DIR = '/usr/lib/dsp'

# Base directory the hexagon-dsp packaging builds one firmware tree per Hexagon
# architecture in, and the sub-directory of a tree the per-DSP directories sit
# under.
#
# This is the first way the firmware directory is found, and the DSP's own
# answer: a FASTRPC_IOCTL_GET_DSP_INFO query on the class's device node reports
# the architecture it runs, so <base>/v<version> is the tree built for it (see
# get_dsp_arch_dir()). A host whose node cannot be opened or queried, or which
# carries no tree for the architecture reported, is answered by the board
# configuration instead.
#
# The per-DSP directories sit one level further down, under a 'dsp'
# sub-directory, which is the same shape a conf.d DSP_LIBRARY_PATH names and so
# the level the DSP_FIRMWARE_FILES globs are resolved against: 'cdsp*' does not
# match the 'dsp' above it.
DSP_ARCH_DIR = '/usr/share/hexagon-dsp/hexagon'
DSP_ARCH_FIRMWARE_SUBDIR = 'dsp'

# Directories the hexagon-dsp packaging drops its board configuration in, and
# the key it names the firmware directory with.
#
# Each conf.d holds one YAML file per board family, naming a relative firmware
# directory per devicetree model:
#
#     machines:
#       Qualcomm Technologies, Inc. Robotics RB3gen2:
#         DSP_LIBRARY_PATH: qcm6490/Thundercomm/RB3gen2/dsp
#
# That is the board's own answer to which firmware it loads, so it is what the
# generator asks after the architecture tree (see DSP_ARCH_DIR): the model read
# off the devicetree picks the entry, and
# DSP_LIBRARY_PATH resolved against the directory holding the conf.d gives the
# host firmware directory (see read_dsp_library_path()). No SoC id table and no
# per-board path compiled in here can go stale against the image the way one
# keyed off /sys/devices/soc0/soc_id did.
#
# Both locations are searched, in this order: the qcom one is where the
# qualcomm-linux images install it and the hexagon-dsp one where the package of
# that name does.
#
# That path is the conf.d of the directory the firmware itself is presented in,
# derived from DSP_FIRMWARE_CONTAINER_DIR rather than spelled out again: the
# configuration describes that firmware, so the two belong at one place in the
# container and cannot drift apart here.
DSP_CONF_DIRS = ('/usr/share/qcom/conf.d', '/usr/share/hexagon-dsp/conf.d')
DSP_CONF_GLOBS = ('*.yaml', '*.yml')
DSP_CONF_MACHINES_KEY = 'machines'
DSP_LIBRARY_PATH_KEY = 'DSP_LIBRARY_PATH'

# Host directory the rpcd services populate with symlinks into the booted
# board's firmware directory, and the glob matching the files in it.
#
# This is the second way to find that directory, for an image with no conf.d:
# cdsprpcd and its siblings symlink dsp/<dsp>/* in here on startup, so
# resolving a symlink leads back to the directory they picked (see
# resolve_dsp_firmware_dirs()). A target that installs real files here instead
# of symlinks resolves to the files themselves, which answers the same
# question.
DSP_SYMLINK_DIR = '/usr/lib/dsp'
DSP_SYMLINK_GLOB = '/usr/lib/dsp/*/*'

# Mount options for the DSP firmware files below, which are the only DSP
# firmware a spec carries: the directories holding them on the host are not
# mounted at all, so a container is given the files its FastRPC client opens
# and no more.
#
# The Hexagon shell is read-write, as the hand-maintained specs these replace
# mount it. The C++ runtime beside it is read-only: it is a shared library the
# container loads, nothing in there has a reason to write to it, and the copy
# behind the mount is the host's own.
DSP_SHELL_MOUNT_OPTIONS = ("nosuid", "rw", "bind")
DSP_LIBRARY_MOUNT_OPTIONS = ("nosuid", "ro", "bind")

# DSP firmware files mounted individually into DSP_FIRMWARE_CONTAINER_DIR, as
# globs relative to a host DSP firmware directory, per device class. Their
# sub-path is preserved, so cdsp/fastrpc_shell_unsigned_3 lands on
# /usr/lib/dsp/cdsp/fastrpc_shell_unsigned_3 and a differently
# versioned shell alongside it keeps its own name.
#
# These are the files the FastRPC client itself opens: the Hexagon shell it
# hands to the DSP loader, and the C++ runtime that shell links against. A file
# with no match is skipped, so a target carrying only the unsigned shell gets
# only that.
#
# The globs are keyed by class because the firmware is per DSP: the shell the
# client hands to the compute DSP lives under cdsp*/, the one it hands to the
# audio DSP under adsp*/ and the one the gdsprpcd service serves under gdsp*/,
# so a class is given its own DSP's firmware and not another's -- which it
# cannot load anyway. The keys are also what says which classes have DSP
# firmware at all, so a class added here is resolved and mounted by main()
# without a second list to keep in step.
#
# The '*' after the DSP name is a wildcard because a multi-DSP target has one
# firmware sub-directory per DSP, each with its own shell: an SA8775P carries
# cdsp/fastrpc_shell_3 next to cdsp1/fastrpc_shell_4, matching its
# /dev/fastrpc-cdsp and /dev/fastrpc-cdsp1 nodes, and gdsp0/ beside gdsp1/ for
# its /dev/fastrpc-gdsp0 and /dev/fastrpc-gdsp1. Each is mounted under its own
# sub-path, since the client picks the directory from the DSP it opened.
#
# Every glob carries the options its own files are mounted with, so which file
# is read-write and which read-only is said where the file is named.
DSP_FIRMWARE_FILES = {
    'fastrpc-adsp': (
        ('adsp*/fastrpc_shell_*', DSP_SHELL_MOUNT_OPTIONS),
        ('adsp*/libc++.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
        ('adsp*/libc++abi.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
    ),
    'fastrpc-cdsp': (
        ('cdsp*/fastrpc_shell_*', DSP_SHELL_MOUNT_OPTIONS),
        ('cdsp*/libc++.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
        ('cdsp*/libc++abi.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
    ),
    'fastrpc-gdsp': (
        ('gdsp*/fastrpc_shell_*', DSP_SHELL_MOUNT_OPTIONS),
        ('gdsp*/libc++.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
        ('gdsp*/libc++abi.so.1', DSP_LIBRARY_MOUNT_OPTIONS),
    ),
}

# Mode for the device node the runtime creates inside the container, carried on
# every deviceNodes entry. 0660 is rw for the owner and rw for the group, which
# with the gid named on the same entry is what lets the container user open the
# node: it reaches the device as a member of the group owning it (see
# generate_devicenodes_cdi() and node_additional_gids()), and a mode that left
# the group bits off would hand it a node it still could not open.
#
# Nothing is granted to others, so the node is no more reachable inside the
# container than the group it belongs to allows, and nothing on the host is
# touched either way -- this is the mode of the node the runtime creates in the
# container, not of the host node it was resolved from.
#
# JSON has no octal literal and the runtimes parse these files as strict JSON,
# so 0o660 is serialised as the decimal 432 that a mode really is. '0660' in
# the file would not parse at all.
DEVICE_NODE_FILE_MODE = 0o660

# Container-side directory to mount a socket into, keyed by the CLASS_SOCKETS
# pattern that matched it on the host. A pattern with no entry here is mounted
# at its own host path.
#
# Every socket is presented to the container at the system-wide location under
# /run, the path a client is pointed at: a Wayland client with
# WAYLAND_DISPLAY=wayland-0 resolves it against XDG_RUNTIME_DIR=/run and
# connects to exactly that socket, a PulseAudio client with
# PULSE_SERVER=unix:/run/pulse/native likewise. Nothing in the container looks
# under /run/user/<uid>, and that directory cannot be handed over wholesale
# either, since mounting it on /run would shadow the container's own /run.
#
# Only the per-user host layouts need an entry here, to pull the socket up to
# that system-wide path; a host already serving it from /run is mounted at its
# own path by the default. So the table names one direction only: host
# /run/user/1000/wayland-0 appears as /run/wayland-0, while host
# /run/wayland-0 stays put.
#
# The basename is preserved, so a second display of one session on
# /run/user/1000/wayland-1 lands on /run/wayland-1 and the two stay distinct.
# Two sessions still land on one path, since each compositor numbers its own
# sockets from zero, as do two audio sockets -- the audio entry names the
# 'pulse' subdirectory because that socket's basename is the constant 'native';
# see generate_mounts_cdi() for how both are kept apart.
#
# The host uid is globbed rather than pinned: the session these sockets belong
# to need not run as uid 1000, and normalising the path away is what keeps the
# host uid out of the container's view.
SOCKET_CONTAINER_DIRS = {
    '/run/user/*/wayland-*': '/run',
    '/run/user/*/pulse/native': '/run/pulse',
    '/run/user/*/pipewire-*': '/run/pipewire',
}

# Host socket patterns whose container path is suffixed when a second socket
# asks for one already taken. A socket matching none of these keeps the path
# socket_container_path() gave it, even if that means two mounts naming it, see
# generate_mounts_cdi().
#
# The per-user sockets of both classes are suffixed, since the remapping drops
# the uid that told two sessions' sockets apart:
#   * an audio socket's basename is the constant 'native', so every session
#     asks for /run/pulse/native and the later ones become
#     /run/pulse/native-1, -2 and so on,
#   * a display socket is numbered by its own compositor from zero, so two
#     sessions both ask for /run/wayland-0 and the later one becomes
#     /run/wayland-0-1.
# Either way every socket stays in the directory a client looks in, no session
# is dropped and none is shadowed, and a container is pointed at one of them
# with PULSE_SERVER=unix:/run/pulse/native-1 or WAYLAND_DISPLAY=wayland-0-1
# without the host uid ever appearing inside the container.
#
# The suffix is a disambiguator and not a session number: the counter runs per
# container path, so the displays of one session need not all carry the same
# suffix (a session whose wayland-1 collides with nothing keeps the bare
# /run/wayland-1 while its wayland-0 is suffixed). The spec's hostPath is what
# says which session a mount came from, and unique_container_path() logs every
# name it assigns.
#
# The system-wide patterns are listed too, so the rule reads per class rather
# than per host layout, though they cannot collide in practice: such a socket
# is mounted at its own host path and one host path is globbed once. A socket
# matching none of these -- the camera server's -- keeps the container path it
# was given, and a host that manages to serve one basename twice gets the later
# mount shadowing the earlier, which is logged.
SOCKET_COLLISION_SUFFIXED = (
    '/run/pulse/native',
    '/run/user/*/pulse/native',
    '/run/pipewire/pipewire-*',
    '/run/user/*/pipewire-*',
    '/run/wayland-*',
    '/run/user/*/wayland-*',
)

# Classes whose spec names every socket it passes through as a device of its
# own, ahead of the '<class>:all' device that hands over the lot.
#
# These are the classes a host can have several sockets of, one session's each:
# a target with two users logged in runs two compositors and two sound servers,
# and a container asking for a display is asking for one of them. A single
# device carrying every mount would hand it all of them at once, with the
# socket of a session it has nothing to do with mounted beside the one it draws
# on.
#
# So each socket is named as its own device, 'audio0' and 'display0' upwards in
# the order find_sockets() returned them, which is lowest session uid first;
# the index is a position in the spec and the mount's hostPath is what says
# which session it came from. A class with a single socket names it after the
# class alone, so the plain 'display' or 'audio' a stock image asks for is
# still what the one server there is answers to.
#
# '<class>:all' is emitted either way, carrying every mount for a container
# that does want every server the host runs.
#
# The camera class is not listed: its server has one system-wide socket and no
# sessions to tell apart, so the class name is already the name of the only
# device there is.
SOCKET_PER_MOUNT_CLASSES = ('audio', 'display')

# The attribute_id naming the Hexagon architecture the DSP runs, and the
# payload FASTRPC_IOCTL_GET_DSP_INFO carries it in:
#
#     struct fastrpc_ioctl_capability {
#         __u32 domain;
#         __u32 attribute_id;
#         __u32 capability;
#         __u32 reserved[4];
#     };
#
# The domain and the attribute go in, the capability comes back and the
# reserved words come back as they were sent (see get_dsp_arch_version()).
ARCH_VER = 6
CAP_STRUCT = struct.Struct("=7I")

# The fastrpc device node and the FastRPC domain of each DSP class, one entry
# per class rather than two mappings keyed alike, so a class cannot be given
# the one without the other.
FASTRPC_DSPS = {
    'fastrpc-adsp': ('/dev/fastrpc-adsp', 0),
    'fastrpc-cdsp': ('/dev/fastrpc-cdsp', 3),
    'fastrpc-gdsp': ('/dev/fastrpc-gdsp0', 5),
}

# The request number the kernel's _IOWR(type, nr, size) encodes, from
# include/uapi/asm-generic/ioctl.h: the direction in the top bits, then the
# payload size, the type and the number. The widths are named as that header
# names them, so the request can be checked against it rather than read as one
# opaque integer.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_TYPESHIFT = _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2

def iowr_request(ioc_type: str, nr: int, size: int) -> int:
    """Return the ioctl request number _IOWR(ioc_type, nr, size) expands to."""
    return ((_IOC_READ | _IOC_WRITE) << _IOC_DIRSHIFT
            | size << _IOC_SIZESHIFT
            | ord(ioc_type) << _IOC_TYPESHIFT
            | nr)

FASTRPC_IOCTL_GET_DSP_INFO = iowr_request('R', 13, CAP_STRUCT.size)

def get_dsp_arch_version(dspclass: str) -> int | None:
    """Return the Hexagon architecture version one class's DSP reports.

    The answer is what FASTRPC_IOCTL_GET_DSP_INFO gives for that class's
    FastRPC domain: 0x68 for a v68 Hexagon, 0x73 for a v73, and so on. It is
    what names the firmware tree built for that architecture (see
    get_dsp_arch_dir()).

    None means the DSP did not answer, which is a host this cannot be asked of
    rather than an error: a class with no fastrpc node of its own, a node that
    is not there, one that refuses to open, or a query the driver rejected.
    The firmware is then looked for the other ways instead.
    """
    devnode, domain = FASTRPC_DSPS.get(dspclass, (None, None))

    if not devnode or not os.path.exists(devnode):
        return None

    # Opened in its own try: the node can be there and still refuse to open,
    # a fastrpc device returning EBUSY/ENODEV while the DSP is still coming up
    # being the case at boot and EACCES the case for a run that is not root.
    # Letting that OSError escape would abort the whole generation from inside
    # the firmware search and cost every other class its spec.
    try:
        fd = os.open(devnode, os.O_RDWR)
    except OSError as err:
        logging.debug("Cannot open %s for the ARCH_VER query: %s", devnode, err)
        return None

    try:
        payload = CAP_STRUCT.pack(domain, ARCH_VER, 0, 0, 0, 0, 0)
        response = fcntl.ioctl(fd, FASTRPC_IOCTL_GET_DSP_INFO, payload, True)
        # The third word is the answer; the rest comes back as it was sent.
        return CAP_STRUCT.unpack(response)[2]
    except OSError as err:
        logging.debug("ARCH_VER query failed for %s: %s", devnode, err)
        return None
    finally:
        os.close(fd)

def get_dsp_arch_dir(dspclass: str) -> str | None:
    """Return the firmware directory built for this class's DSP architecture.

    The version the DSP reports names a tree of its own, so the directory is
    DSP_ARCH_DIR/v<version>/dsp and the per-DSP sub-directories sit inside it
    (see DSP_ARCH_DIR). This is the first of the ways the firmware directory is
    found.

    None means there is no such directory to hand over: a DSP that did not
    report its architecture, or a host carrying no tree for the one it did.
    """
    capability = get_dsp_arch_version(dspclass)

    if capability is None:
        logging.info("%s: unable to query ARCH_VER", dspclass)
        return None

    logging.info("%s: ARCH_VER raw=%u (0x%x)", dspclass, capability, capability)

    arch = f"v{capability & 0xff:x}"

    path = str(PurePosixPath(DSP_ARCH_DIR, arch, DSP_ARCH_FIRMWARE_SUBDIR))

    logging.info("%s: checking architecture directory %s", dspclass, path)

    if os.path.isdir(path):
        logging.info("%s: using architecture directory %s", dspclass, path)
        return path

    logging.info("%s: architecture directory not found: %s", dspclass, path)

    return None

def get_firmware_search_dirs(
        dspclass: str,
        boardconf,
        machinename) -> list[str]:

    dirs = []

    # The architecture directory the DSP itself reports, asked first.
    archdir = get_dsp_arch_dir(dspclass)

    if archdir:
        dirs.append(archdir)

    # The directory the board configuration names, asked second.
    boarddirs = dsp_firmware_board_dirs(
        boardconf,
        machinename)

    dirs.extend(boarddirs)

    return dirs

def node_gid_map(cdiclass: str, nodes: list[str] | None = None,
                 gid: int | None = None, what: str = "nodes") -> dict[str, int]:
    """Map each device node of one class to the group to grant for it.

    An explicit --gid always wins, for every node. Otherwise each node's group
    is read from the node itself, because that is the group the driver and its
    udev rules actually hand the device to, and it differs per board: a render
    node may be 'render' on one target and the container user's primary group
    on another.

    The group is resolved per node because one class can span several groups on
    a single board: a target may expose /dev/video0 in 'video' while a node
    alongside it belongs to a vendor group. Collapsing those to one gid would
    leave every node outside the winning group unopenable, so each node keeps
    its own and build_cdi_spec() grants the union (see node_additional_gids).

    That read is the only thing a gid comes from. A node that could not be
    stat()ed -- one that disappeared between the glob and the stat -- is left
    out of the map, and so is a node owned by group root: the runtime drops a 0
    additionalGids entry, so a 0 would grant an unprivileged container nothing.
    Either way the node is emitted without a gid and no group is granted for
    it.

    Nothing stands in for a group that could not be read. A spec names the
    groups owning the paths it hands over and no group picked for the
    container, and no number would be the right one to pick anyway: 1000 is the
    container user's primary group under qualcomm-linux (user 'weston') but not
    under every image, Debian/Ubuntu having user 'ubuntu' at uid 1000 with
    primary gid 1001. A container that needs a group the host paths do not name
    is given it on the command line, where a choice about its own identity
    belongs, or by --gid here.

    what names the paths in the log lines; the socket-backed classes pass
    'sockets' since they have no device node to speak of.
    """
    gidmap = {}
    for node in sorted(nodes or []):
        if gid is not None:
            gidmap[node] = gid
            continue
        try:
            nodegid = os.stat(node).st_gid
        except OSError as err:
            logging.debug("Cannot stat %s for its group, naming none: %s",
                          node, err)
            continue
        if not nodegid:
            logging.debug("%s is owned by group root, naming no group for it",
                          node)
            continue
        gidmap[node] = nodegid
    bygid = {}
    for node, nodegid in gidmap.items():
        bygid.setdefault(nodegid, []).append(node)
    if len(bygid) > 1:
        logging.info("Groups for '%s' %s: %s", cdiclass, what,
                     ", ".join("%d (%d path(s))" % (g, len(group))
                               for g, group in sorted(bygid.items())))
    elif bygid:
        logging.info("Group for '%s' %s: %d", cdiclass, what, next(iter(bygid)))
    return gidmap

def node_additional_gids(gids: int | list[int] | None = None) -> list[int]:
    """Return the additionalGids to grant for one device class.

    The host resources a class passes through are group accessible rather than
    world accessible -- a sound server's socket is mounted with the host's
    permissions on it, and a device node the runtime bind-mounts instead of
    re-creating keeps the host's group too. Adding those gids to the container's
    supplementary groups makes them reachable without relaxing a mode on either
    side, which is the whole of what the spec says about access.

    Every group found on the class's paths is granted, not just one:
    additionalGids is a spec-level array, so a class whose paths span several
    groups needs all of them. Granting only the most common one would leave the
    rest unreachable for exactly the paths that differ.

    Nothing is granted beyond those groups. A spec names what owns the paths
    the class hands over, so the container is put into the groups it needs to
    reach them and into no group picked for it -- which is also what makes
    --gid a replacement rather than an addition, the gids it names standing
    alone.

    A class whose paths named no group at all therefore grants nothing: the
    gids are what node_gid_map() read off the host and there is nothing else
    for them to be.

    gid 0 is dropped: the runtime skips a 0 entry when it builds the OCI spec,
    so emitting it would only add a no-op to every generated file.
    """
    if gids is None:
        gids = []
    elif isinstance(gids, int):
        gids = [gids]
    else:
        gids = list(gids)
    return sorted({nodegid for nodegid in gids if nodegid})

def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(format="%(levelname)s: %(message)s", level=level)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Qualcomm CDI specification files")
    parser.add_argument("-d", "--destdir", default="/", help="Destination root directory (default: %(default)s)")
    parser.add_argument("-c", "--cdifilename", default="qualcomm.json", help="CDI JSON filename base for the qualcomm.com specs; the device class is inserted before the extension, e.g. qualcomm.json -> qualcomm-gpu.json. A spec in another vendor namespace is named after that vendor instead, e.g. linux-audio.json (default: %(default)s)")
    parser.add_argument("-C", "--classes", default=None, help="Comma-separated list of CDI classes to generate (default: all). Available: %s" % ", ".join(KNOWN_CLASSES))
    parser.add_argument("-g", "--gid", type=int, default=None, metavar="GID", help="gid to grant via additionalGids, replacing the group read from each host device node and socket")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Parse and probe devices but do not write any files")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv)")
    return parser.parse_args()

def is_unix_socket(path: str) -> bool:
    """Return whether path refers to a unix domain socket."""

    try:
        mode = os.stat(path, follow_symlinks=True).st_mode
    except OSError as err:
        logging.debug("Cannot stat %s: %s", path, err)
        return False

    return stat.S_ISSOCK(mode)

def is_character_device(path: str) -> bool:
    """Return whether path refers to a character device."""

    try:
        mode = os.stat(path, follow_symlinks=True).st_mode
    except OSError as err:
        logging.debug("Cannot stat %s: %s", path, err)
        return False

    return stat.S_ISCHR(mode)

def find_devicenodes(deviceglob: str) -> list[str]:
    logging.debug("Globbing device nodes: %s", deviceglob)
    files = [
        path for path in glob.glob(deviceglob)
        if is_character_device(path)
    ]

    logging.info("Found %d nodes for pattern %s", len(files), deviceglob)
    logging.debug("Nodes for %s: %s", deviceglob, files)

    return files

def find_sockets(cdiclass: str) -> list[str]:
    """Return the host sockets to mount for one class, keeping only those present.

    The class's patterns (see CLASS_SOCKET_GROUPS) are globbed in order. A
    system-wide socket under /run is preferred over its per-user fallback: if
    a pattern finds one or more sockets, the immediately following fallback
    pattern is skipped. This lets a host exposing both /run/pulse/native and
    /run/user/<uid>/pulse/native contribute only the system-wide socket, while
    still allowing different socket families such as PulseAudio and PipeWire to
    be collected together.

    All matches of a selected pattern are kept, since one pattern can
    legitimately name several sockets (a compositor exposing wayland-0 and
    wayland-1, or one per logged-in user), except that a
    SOCKET_IGNORE_SUFFIXES path is not a socket at all and is dropped before
    the pattern contributes anything.

    The sockets are returned lowest session uid first, so which one keeps the
    unsuffixed container path does not depend on the order they were found in
    (see socket_session_order()).

    No match from any pattern means the host-side server is not running, so
    there is nothing for the container to connect to and the class is skipped.
    This is the socket equivalent of an empty device node glob.
    """
    sockets = []

    for group in CLASS_SOCKET_GROUPS.get(cdiclass, ()):
        for pattern in group:
            matches = []
            for path in sorted(glob.glob(pattern)):
                if path.endswith(SOCKET_IGNORE_SUFFIXES):
                    logging.debug("Not a %s socket, ignoring %s", cdiclass, path)
                    continue
                if not is_unix_socket(path):
                    logging.debug("Path %s matches %s but is not a socket",
                                  path, pattern)
                    continue

                matches.append(path)

            if matches:
                sockets.extend(matches)
                break

    sockets = sorted(sockets, key=socket_session_order)

    for path in sockets:
        logging.info("Found %s socket: %s", cdiclass, path)

    return sockets

def socket_runtime_uid(socket: str) -> int | None:
    """Return the uid whose runtime directory holds a socket.

    None for a socket that is not in a per-user runtime directory at all, which
    is a server running system-wide: /run/wayland-0 belongs to no session and
    has no uid to read, while /run/user/1000/wayland-0 gives 1000.

    The uid is read from the path rather than stat()ed off the socket because
    it is the directory that decides which session a socket belongs to, and
    that is what the caller is choosing between. A path whose uid component is
    not a number is treated as having none: /run/user is only globbed for the
    numbered directories logind creates there.
    """
    parts = PurePosixPath(socket).parts
    if parts[1:3] == ('run', 'user') and len(parts) > 3 and parts[3].isdigit():
        return int(parts[3])
    return None

def socket_session_order(socket: str) -> tuple[int, str]:
    """Return the sort key putting the lowest-uid session's sockets first.

    The order decides which socket keeps the unsuffixed container path when two
    sessions ask for one (see generate_mounts_cdi()), so it is the uid that is
    compared and not the path: the paths sort as strings, where /run/user/10000
    comes before /run/user/1002, and the display a stock image connects to with
    no configuration should not hinge on that. The path breaks a tie, keeping
    the sockets of one session in the order the compositor numbered them.

    A socket with no runtime uid is served system-wide and sorts first, ahead
    of any session's. Nothing mixes the two in practice -- find_sockets() stops
    at one pattern, so a class carries either layout and not both.
    """
    sessionuid = socket_runtime_uid(socket)
    return (-1 if sessionuid is None else sessionuid, socket)

def find_dsp_conf_dirs() -> list[str]:
    """Return the hexagon-dsp conf.d directories this host has.

    Both DSP_CONF_DIRS are looked for, in order, so an image that installs the
    configuration under either name is found. A host with neither has no board
    configuration to read and is answered by the symlinks instead (see
    resolve_dsp_firmware_dirs()).
    """
    confdirs = [confdir for confdir in DSP_CONF_DIRS
                if os.path.isdir(confdir)]
    for confdir in confdirs:
        logging.info("Found DSP configuration directory: %s", confdir)
    if not confdirs:
        logging.info("No DSP configuration directory (%s)",
                     ", ".join(DSP_CONF_DIRS))
    return confdirs

def parse_dsp_conf(conffile: str) -> dict[str, dict[str, str]]:
    """Return the machines map of one hexagon-dsp configuration file.

    The file is YAML and is read as YAML, so the SPDX header and the schema
    annotation it carries cost nothing, quoting and indentation are the
    parser's to resolve, and a file the packaging reshapes within the format
    is still read the way the rest of the image reads it.

    Only the one shape this needs is taken from the document: the
    DSP_CONF_MACHINES_KEY mapping of devicetree model to a mapping of keys,
    DSP_LIBRARY_PATH among them. Every other section is passed over, so what
    else an image chooses to keep in there is none of this generator's
    business.

    safe_load() rather than load(): the file is a board description to read,
    not code to run, and nothing in it may construct a Python object.

    An empty map answers a file that cannot be read, is not valid YAML, or is
    not shaped this way. None of the three stops the run: the firmware is
    resolvable from the host's own symlinks as well (see
    resolve_dsp_firmware_dirs()), so an unreadable configuration costs the
    second-best answer and not the whole generation.

    The names and the settings are returned as text, since a model name is
    only ever compared and a library path only ever joined -- a bare 'yes' or
    a bare number in there is YAML's to type and not this generator's to carry
    a non-string through.
    """
    try:
        with open(conffile, "r") as conf:
            document = yaml.safe_load(conf)
    except OSError as err:
        logging.info("Cannot read the DSP configuration %s: %s", conffile, err)
        return {}
    except yaml.YAMLError as err:
        logging.warning("Cannot parse the DSP configuration %s: %s",
                        conffile, err)
        return {}
    machines = (document.get(DSP_CONF_MACHINES_KEY)
                if isinstance(document, dict) else None)
    if not isinstance(machines, dict):
        logging.debug("%s holds no '%s' mapping", conffile,
                      DSP_CONF_MACHINES_KEY)
        return {}
    return {str(machine): {str(key): str(value)
                           for key, value in settings.items()}
            for machine, settings in machines.items()
            if isinstance(settings, dict)}

def read_dsp_library_path(machinename: str | None,
                          confdirs: list[str]) -> tuple[str, str] | None:
    """Return (conf.d directory, DSP_LIBRARY_PATH) for this board, or None.

    Every configuration file in confdirs is read in turn and the first one
    naming this board's devicetree model wins, which is what resolves the
    firmware directory the image itself says the board loads.

    The conf.d directory is returned alongside the path because both follow
    from it: the directory holding it is what the relative path resolves
    against, so the qcom conf.d gives /usr/share/qcom/<DSP_LIBRARY_PATH> and
    the hexagon-dsp one /usr/share/hexagon-dsp/<DSP_LIBRARY_PATH>.

    None means no file named this model -- a board the image has no entry for,
    or a host with no model to match -- and the firmware is then looked for the
    other ways (see dsp_firmware_board_dirs()).
    """
    if not machinename:
        return None
    for confdir in confdirs:
        for confglob in DSP_CONF_GLOBS:
            for conffile in sorted(glob.glob(str(PurePosixPath(confdir,
                                                               confglob)))):
                machines = parse_dsp_conf(conffile)
                settings = machines.get(machinename)
                if not settings:
                    logging.debug("%s names no entry for '%s'",
                                  conffile, machinename)
                    continue
                librarypath = settings.get(DSP_LIBRARY_PATH_KEY)
                if not librarypath:
                    logging.info("%s names '%s' but sets no %s", conffile,
                                 machinename, DSP_LIBRARY_PATH_KEY)
                    continue
                logging.info("%s sets %s=%s for '%s'", conffile,
                             DSP_LIBRARY_PATH_KEY, librarypath, machinename)
                return confdir, librarypath
    return None

def resolve_dsp_firmware_dirs() -> list[str]:
    """Return the firmware directories the host's DSP symlinks point into.

    DSP_SYMLINK_GLOB matches what the rpcd services put in DSP_SYMLINK_DIR,
    one symlink per firmware file, so resolving them leads back to the board
    directory those services picked -- the same answer the configuration would
    have given, for an image that carries no configuration. A target that
    installs real files there rather than symlinks resolves to the files
    themselves, which says the same thing.

    This is the last place the firmware is looked for: a host where neither
    this nor the configuration names a directory gets no firmware mounts, and
    nothing is guessed at on its behalf.

    The directory returned is the one the per-DSP sub-directories sit in, two
    levels up from a resolved file, so it is the same kind of path a conf.d
    DSP_LIBRARY_PATH names and the per-DSP sub-path of every file is kept.
    """
    dspdirs = []
    for link in sorted(glob.glob(DSP_SYMLINK_GLOB)):
        try:
            target = os.path.realpath(link)
        except OSError as err:
            logging.debug("Cannot resolve %s: %s", link, err)
            continue
        if not os.path.exists(target):
            logging.debug("%s points at %s, which is not there", link, target)
            continue
        dspdir = str(PurePosixPath(target).parent.parent)
        if dspdir not in dspdirs:
            logging.info("%s resolves the DSP firmware directory %s",
                         link, dspdir)
            dspdirs.append(dspdir)
    return dspdirs

def dsp_firmware_board_dirs(boardconf: tuple[str, str] | None,
                            machinename: str | None) -> list[str]:
    """Return this board's own DSP firmware directories, best answer first.

    boardconf is what the board configuration said, if anything (see
    read_dsp_library_path()): a conf.d entry for this devicetree model names
    the firmware directory the image itself says the board loads, which is the
    one answer that cannot be a guess. The relative path it holds is resolved
    against the directory holding that conf.d.

    A host with no configuration, or none naming this board, falls back to
    resolving the symlinks the rpcd services left in DSP_SYMLINK_DIR. That is
    the directory the host's own stack picked, so it answers the same question
    a step further removed.

    An empty list means neither said anything, and the fastrpc specs then
    carry no firmware at all: nothing is looked for that the host did not point
    at (see generate_dsp_files_cdi()). machinename is carried for warning messages.
    """
    if boardconf:
        confdir, librarypath = boardconf
        boarddir = str(PurePosixPath(confdir).parent.joinpath(librarypath))
        if os.path.isdir(boarddir):
            return [boarddir]
        logging.info("%s names %s, which is not a directory on this host",
                     DSP_LIBRARY_PATH_KEY, boarddir)
    boarddirs = resolve_dsp_firmware_dirs()
    if not boarddirs:
        logging.warning("Neither the DSP configuration nor %s names a firmware "
                        "directory for '%s'; the fastrpc specs carry no "
                        "firmware", DSP_SYMLINK_DIR, machinename)
    return boarddirs

def find_dsp_firmware_files(cdiclass: str,
                            dspdir: str) -> list[tuple[str, list[str]]]:
    """Return one class's firmware files inside one host firmware directory.

    The DSP_FIRMWARE_FILES globs of cdiclass are resolved against dspdir, so
    the result is the firmware of that class's own DSP and nothing else. An
    empty list means this directory holds no firmware for the class: another
    board's directory, or this board's for a DSP it does not have.

    Each file is returned with the mount options of the glob that matched it,
    which is what mounts the Hexagon shell read-write and the C++ runtime
    read-only without a second table keyed off file names (see
    DSP_FIRMWARE_FILES).

    This answers both questions asked about a candidate directory -- whether it
    is the one to resolve the class from, and which per-DSP sub-directories to
    mount out of it -- from the files themselves rather than from a second
    table of directory names to keep in step.
    """
    found = []
    for fileglob, options in DSP_FIRMWARE_FILES.get(cdiclass, []):
        pattern = str(PurePosixPath(dspdir, fileglob))
        logging.debug("Globbing DSP files: %s", pattern)
        found += [(hostpath, options) for hostpath in sorted(glob.glob(pattern))]
    return found

def glob_matches(pattern: str, path: str) -> bool:
    """Report whether path is something glob(pattern) could have returned.

    fnmatch alone would let a '*' span '/', so '/run/wayland-*' would also
    match '/run/wayland-dir/socket'. Compare segment by segment instead, which
    is how glob treats a wildcard.
    """
    patternparts = pattern.split('/')
    pathparts = path.split('/')
    return len(patternparts) == len(pathparts) and all(
        fnmatch.fnmatchcase(pathpart, patternpart)
        for patternpart, pathpart in zip(patternparts, pathparts))

def socket_container_path(socket: str) -> str:
    """Return the in-container path to mount one host socket at.

    Defaults to the host path itself, which for a socket served system-wide
    from /run is already where a client in the container looks. A socket
    matching a SOCKET_CONTAINER_DIRS pattern is instead placed in that
    directory under the same basename, which is what lifts a per-user runtime
    socket to its /run location, so the container resolves one path whatever
    the host layout is.
    """
    for pattern, containerdir in SOCKET_CONTAINER_DIRS.items():
        if glob_matches(pattern, socket):
            remapped = str(PurePosixPath(containerdir, PurePosixPath(socket).name))
            logging.debug("Mounting %s into the container at %s", socket, remapped)
            return remapped
    return socket

def generate_mounts_cdi(sockets: list[str]) -> list[dict]:
    """Build the containerEdits mount entries for a list of host sockets.

    Each socket is bind-mounted where a client in the container resolves it
    with no extra configuration: at its system-wide /run path, which is its own
    host path for a host serving it there and a remapped one for a host keeping
    it in a per-user runtime directory (see socket_container_path()). The
    mounts are read-write because a unix socket cannot be used by a client that
    may not write to it.

    Two host sockets can want the same container path, because the remapping
    keeps only the basename, and a host with two sessions has that both ways:
    /run/user/1000/pulse/native and /run/user/1001/pulse/native both ask for
    /run/pulse/native, since 'native' is a constant and the uid was all that
    told them apart, and /run/user/1000/wayland-0 and /run/user/1001/wayland-0
    both ask for /run/wayland-0, since each compositor numbers its own sockets
    from zero. The sockets of a single session do not collide: there one
    compositor does the numbering, so wayland-0 and wayland-1 stay distinct.

    Either is therefore suffixed by unique_container_path() -- both match
    SOCKET_COLLISION_SUFFIXED -- giving /run/pulse/native-1 and
    /run/wayland-0-1, so no session's socket is lost, none is shadowed, and
    every one of them still sits in the directory a client looks in. The
    unsuffixed path is the one a stock image connects to with no configuration,
    and it goes to the lowest-uid session because find_sockets() returns them
    in that order; a container wanting another is pointed at it with
    PULSE_SERVER=unix:/run/pulse/native-1 or WAYLAND_DISPLAY=wayland-0-1.

    Any other socket keeps the container path it was given. Two of them on one
    path is then a host this generator did not expect, so the spec carries both
    mounts, the later shadowing the earlier, and says so.
    """
    mounts = []
    taken = {}
    for socket in sockets:
        containerpath = socket_container_path(socket)
        if containerpath in taken:
            if socket_suffixed_on_collision(socket):
                containerpath = unique_container_path(containerpath, set(taken))
            else:
                logging.warning("Container path %s is mounted from %s already, "
                                "so %s shadows it inside the container",
                                containerpath, taken[containerpath], socket)
        taken[containerpath] = socket
        mounts.append({"hostPath": socket, "containerPath": containerpath,
                       "options": list(SOCKET_MOUNT_OPTIONS)})
    return mounts

def socket_suffixed_on_collision(socket: str) -> bool:
    """Say whether a socket's container path is suffixed when it is taken.

    True for a socket matching SOCKET_COLLISION_SUFFIXED, the audio and display
    ones, whose sessions are told apart by nothing else once the remapping has
    dropped the uid. False for every other socket, the camera server's, which
    keeps the container path it was given and shadows whatever is mounted
    there.
    """
    return any(glob_matches(pattern, socket)
               for pattern in SOCKET_COLLISION_SUFFIXED)

def unique_container_path(containerpath: str, taken: set[str]) -> str:
    """Return containerpath, suffixed if it is already taken by another mount.

    The suffix is '-<n>' appended to the basename, counting from 1 and skipping
    whatever is taken already, so a third socket asking for /run/pulse/native
    becomes /run/pulse/native-2 rather than reusing the /run/pulse/native-1 the
    second one took. The counter is also what keeps a host that really does
    serve /run/pulse/native-1 itself from being collided with by a generated
    name.

    The directory is left alone and only the last path component is suffixed,
    since it is the mount point's own name that has to be distinct; suffixing
    the directory instead would put the socket somewhere the container does not
    look.
    """
    if containerpath not in taken:
        return containerpath
    base = PurePosixPath(containerpath)
    index = 1
    while True:
        candidate = str(base.with_name("%s-%d" % (base.name, index)))
        if candidate not in taken:
            logging.info("Container path %s is already mounted, using %s",
                         containerpath, candidate)
            return candidate
        index += 1

def generate_dsp_files_cdi(cdiclass: str, dspdirs: list[str]) -> list[dict]:
    """Build the mounts for the DSP files the container resolves by fixed path.

    dspdirs are the firmware directories this board answered with (see
    dsp_firmware_board_dirs()), searched in order, and the first one holding
    this class's firmware is the one it is mounted from: a second directory
    would put two mounts on one container path, which is a conflicting spec.
    There is normally one to search anyway, a board naming its own directory.

    Every DSP_FIRMWARE_FILES match in it is mounted into
    DSP_FIRMWARE_CONTAINER_DIR under the same per-DSP sub-path, so the FastRPC
    stack in the container finds the Hexagon shell and its C++ runtime at one
    fixed location whatever board-specific directory the host kept them in.
    Each is mounted with the options of the glob that named it, the shell
    read-write and the runtime read-only (see DSP_FIRMWARE_FILES).

    These are the only firmware mounts a spec carries: the host directory they
    were found in is not mounted, so what reaches the container is the files
    its client opens and not the board directory around them.

    Only the globs of cdiclass are searched, so each fastrpc class carries the
    firmware of its own DSP: the cdsp*/ files reach the fastrpc-cdsp spec, the
    adsp*/ files the fastrpc-adsp one and the gdsp*/ files the fastrpc-gdsp
    one. A class with no entry in DSP_FIRMWARE_FILES mounts no file
    individually.

    No mounts at all means this host said nothing about where that DSP's
    firmware is, or said it about a directory holding none of it, and the
    class's spec carries its device nodes without it.
    Nothing is looked for past what the board pointed at: on an image built
    for a family of targets that would mean another board's firmware, which is
    worse than none, being exactly the mismatch mounting the host's own copies
    avoids.
    """
    for dspdir in dspdirs:
        firmware = find_dsp_firmware_files(cdiclass, dspdir)
        if not firmware:
            logging.debug("The board directory %s holds no '%s' firmware",
                          dspdir, cdiclass)
            continue
        logging.info("Resolving '%s' firmware from %s", cdiclass, dspdir)
        mounts = {}
        for hostpath, options in firmware:
            relative = PurePosixPath(hostpath).relative_to(PurePosixPath(dspdir))
            containerpath = str(PurePosixPath(DSP_FIRMWARE_CONTAINER_DIR,
                                              relative))
            if containerpath in mounts:
                logging.debug("Already mounting %s at %s, skipping %s",
                              mounts[containerpath]["hostPath"],
                              containerpath, hostpath)
                continue
            logging.info("Mounting DSP file %s into the container at %s",
                         hostpath, containerpath)
            mounts[containerpath] = {
                "hostPath": hostpath,
                "containerPath": containerpath,
                "options": list(options),
            }
        return list(mounts.values())
    logging.info("No '%s' firmware for this board%s", cdiclass,
                 " in " + ", ".join(dspdirs) if dspdirs else "")
    return []

def read_device_model() -> str | None:
    """Return the board name the host devicetree model node holds.

    This is the board name used when resolving the DSP firmware configuration.

    None means there is no board name to use, from a host with no model node
    at all or one whose node could not be read.

    The node is NUL-terminated and the terminator is dropped, along with any
    whitespace around the name.
    """
    try:
        with open('/sys/firmware/devicetree/base/model', "r") as modelnode:
            machinename = modelnode.read().replace('\x00', '').strip()
    except OSError as err:
        logging.debug("Cannot read the board name from devicetree: %s", err)
        return None
    if not machinename:
        logging.debug("The devicetree model holds no board name")
        return None
    logging.debug("Read board name from devicetree: %s", machinename)
    return machinename

def generate_audio_env_entries(sockets: list[str]) -> list[str]:
    """Build env entries for the audio CDI spec."""

    env = []

    pulse_server = os.environ.get("PULSE_SERVER")
    pipewire_remote = os.environ.get("PIPEWIRE_REMOTE")
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")

    if pulse_server:
        env.append(f"PULSE_SERVER={pulse_server}")
    else:
        for socket in sockets:
            container_path = socket_container_path(socket)
            if PurePosixPath(container_path).name == "native":
                env.append(f"PULSE_SERVER=unix:{container_path}")
                break

    if pipewire_remote:
        env.append(f"PIPEWIRE_REMOTE={pipewire_remote}")
    else:
        for socket in sockets:
            container_path = socket_container_path(socket)
            if PurePosixPath(container_path).name.startswith("pipewire-"):
                env.append(f"PIPEWIRE_REMOTE={container_path}")
                break

    if xdg_runtime_dir:
        env.append(f"XDG_RUNTIME_DIR={xdg_runtime_dir}")
    else:
        env.append("XDG_RUNTIME_DIR=/run")

    return env

def generate_display_env_entries(sockets: list[str]) -> list[str]:
    """Build env entries for the display CDI spec."""

    env = []

    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    wayland_display = os.environ.get("WAYLAND_DISPLAY")

    if wayland_display:
        env.append(f"WAYLAND_DISPLAY={wayland_display}")
    else:
        for socket in sockets:
            container_path = socket_container_path(socket)

            if PurePosixPath(container_path).name.startswith("wayland-"):
                env.append(
                    f"WAYLAND_DISPLAY={PurePosixPath(container_path).name}"
                )
                break

    if xdg_runtime_dir:
        env.append(f"XDG_RUNTIME_DIR={xdg_runtime_dir}")
    else:
        env.append("XDG_RUNTIME_DIR=/run")

    return env

def generate_sockets_cdi(nickname: str, sockets: list[str]) -> list[dict]:
    """Build the CDI device entries for a socket-only class.

    The camera, audio and display classes pass through no device node at all,
    so their device entries carry socket mounts rather than a deviceNodes list.
    The container asks for linux.org/audio=audio and gets the socket; without a
    device entry the class could not be requested on the command line at all,
    since a runtime only exposes named devices.

    A SOCKET_PER_MOUNT_CLASSES class names every socket as a device of its own,
    'audio0' and 'display0' upwards, so a container can ask for one session's
    server instead of every server the host runs. With a single socket the
    class name is the device name, so the plain 'display' or 'audio' a stock
    image asks for still names the one server there is. Any other class -- the
    camera, whose server has a single system-wide socket -- is one device
    carrying its mounts.

    Every class then ends with the '<class>:all' catch-all carrying all of them
    at once, the same spelling the device-node classes provide.
    """
    if not sockets:
        logging.debug("No sockets for '%s'; no CDI entries generated", nickname)
        return []
    logging.info("Generating CDI entries for '%s' with %d socket(s)",
                 nickname, len(sockets))
    mounts = generate_mounts_cdi(sockets)
    devices = []
    if nickname in SOCKET_PER_MOUNT_CLASSES:
        for index, mount in enumerate(mounts):
            # One device per socket, named by its position among them, or
            # after the class itself when it is the only socket there is.
            name = nickname if len(mounts) == 1 else nickname + str(index)
            device = {"name": name, "containerEdits": {"mounts": [mount]}}
            logging.debug("CDI socket entry: %s", device)
            devices.append(device)
    else:
        devices.append({"name": nickname,
                        "containerEdits": {"mounts": mounts}})
    devices.append({"name": nickname + ":all",
                    "containerEdits": {"mounts": mounts}})
    return devices

def generate_devicenodes_cdi(nickname: str, filesglob: list[str],
                             pernode: bool = True,
                             catchall: bool = True,
                             gidmap: dict[str, int] | None = None) -> list[dict]:
    """Build the CDI device entries for a list of device nodes.

    nickname:    base name for the emitted devices, suffixed with the node index
                 or, for a vendor-qualified node name, with the vendor
    filesglob:   the device nodes to expose
    pernode:     emit one device per node (renderD128, video0, ...)
    catchall:    emit the '<nickname>:all' device covering every node at once
    gidmap:      the group to name for each node, from node_gid_map()

    A deviceNodes entry is the path, the node's group and the mode to create it
    with. The type and major/minor the runtime still fills in from the host
    node it resolves, and no uid is named, so the node keeps the identity the
    host gave it in every respect the spec does not spell out.

    The gid is the group that owns the node on this board, the same one the
    class grants through additionalGids (see node_additional_gids()), and the
    mode is DEVICE_NODE_FILE_MODE. Both describe the node the runtime creates
    inside the container, not the host node: together they make it openable by
    a container user that joined the group, on a runtime that creates the node
    fresh rather than carrying the host's ownership and mode over with it.
    Nothing on the host is read other than the group, and nothing on it is
    changed.

    A node missing from gidmap is emitted without a gid, there being no group
    read for it to name, and keeps the mode.

    A class assembled from several globs calls this once per glob, with
    catchall=False on all but one so the ':all' device is not defined twice, and
    a final pernode=False call to emit a single catch-all spanning the lot.
    """
    if filesglob:
        logging.info("Generating CDI entries for '%s' with %d node(s)", nickname, len(filesglob))
        filesglob_set = set(filesglob)

        # -secure nodes whose non-secure parent is also present are handled as siblings of
        # that parent entry, not as independent top-level entries
        def is_sibling(node: str) -> bool:
            return node.endswith('-secure') and node[:-len('-secure')] in filesglob_set

        def node_entry(node: str) -> dict:
            # One deviceNodes entry: the path, the group resolved for that node
            # where there is one, and the mode to create the node with. The gid
            # is per node because one class can span several groups on a board,
            # which is the same reason node_gid_map() resolves it per node; the
            # mode is the same for every node (see DEVICE_NODE_FILE_MODE).
            entry = {"path": node}
            nodegid = (gidmap or {}).get(node)
            if nodegid is not None:
                entry["gid"] = nodegid
            entry["fileMode"] = DEVICE_NODE_FILE_MODE
            return entry

        def node_paths(node: str) -> list[dict]:
            # deviceNodes entries for one node: the node itself, plus its
            # -secure sibling when that sibling is present. The condition is the
            # exact inverse of is_sibling(), so every node it suppresses as a
            # top-level entry is placed here and none can be lost -- a
            # multi-DSP target's /dev/fastrpc-cdsp1-secure rides on
            # /dev/fastrpc-cdsp1 the way the unindexed pair does.
            paths = [node_entry(node)]
            securepath = node + "-secure"
            if securepath in filesglob_set:
                logging.debug("Adding -secure variant for %s", node)
                paths.append(node_entry(securepath))
            return paths

        def node_vendor(node: str) -> str | None:
            # The vendor a devicetree-style node name is qualified with:
            # /dev/dma_heap/qcom,system is the vendor's spelling of the plain
            # 'system' heap, and the part before the comma is what tells the
            # two apart. Returning it names the device after the vendor
            # ('dmaheap-system-qcom') instead of by a position in the glob,
            # which says nothing about which heap it is and moves when a node
            # appears or goes away.
            vendor, comma, _ = PurePosixPath(node).name.partition(',')
            return vendor if comma and vendor else None

        # Count only nodes that will be named from their index, i.e. those that
        # produce an entry of their own and have no vendor to be named after
        effective_count = sum(1 for n in filesglob
                              if not is_sibling(n) and node_vendor(n) is None)

        devicenodelist = []
        devicenodeindex = 0
        for devicenode in sorted(filesglob) if pernode else []:
            if is_sibling(devicenode):
                continue
            device_pathlist = { "deviceNodes": node_paths(devicenode) }
            cdi_index = get_devicenode_index(devicenode)
            vendor = node_vendor(devicenode)
            # If there's only one match *and* it doesn't have its own index, don't add the '0' index
            if effective_count == 1 and cdi_index is None and vendor is None:
                # Empty string means str(cdi_index) appends nothing, giving just 'nickname'
                cdi_index = ""
            # Name a vendor-qualified node after its vendor, keeping any index
            # the name carries so two of them cannot collide
            if vendor is not None:
                suffix = "-" + vendor + ("" if cdi_index is None else str(cdi_index))
                device_entry = { "name": nickname+suffix, "containerEdits": device_pathlist }
            # Reuse the devicenode index if present, otherwise generate our own
            elif cdi_index is not None:
                # cdi_index is either an int parsed from the node name or "" (single unnamed node)
                device_entry = { "name": nickname+str(cdi_index), "containerEdits": device_pathlist }
            else:
                # No index in the node name and multiple nodes: fall back to a sequential counter
                device_entry = { "name": nickname+str(devicenodeindex), "containerEdits": device_pathlist }
            logging.debug("CDI device entry: %s", device_entry)
            devicenodelist.append(device_entry)
            devicenodeindex += 1

        # Build a catch-all entry that exposes every node in this class at once;
        # useful when a container needs the full set without listing them individually
        if catchall:
            device_paths = []
            for devicenode in sorted(filesglob):
                if is_sibling(devicenode):
                    continue
                # Reuse the per-device logic so cdsp-secure/adsp-secure is also in :all
                device_paths.extend(node_paths(devicenode))
            device_pathlist = { "deviceNodes":  device_paths  }
            device_entries = { "name": nickname+":all", "containerEdits": device_pathlist }
            logging.debug("CDI catch-all entry: %s", device_entries)
            devicenodelist.append(device_entries)
    else:
        devicenodelist = []
        logging.debug("No nodes found for '%s'; no CDI entries generated", nickname)
    return devicenodelist

def get_devicenode_index(nodename: str) -> int | None:
    # Extract a trailing integer from the node name (e.g. 128 from 'renderD128')
    nodeindex = re.search(r'\d+$', nodename)
    return int(nodeindex.group()) if nodeindex else None

def class_vendor(cdiclass: str) -> str:
    """Return the CDI vendor namespace one class's spec is emitted under.

    CLASS_VENDOR names the classes that are not in the Qualcomm namespace --
    the kernel's video interface and the two socket-backed Linux servers --
    and every other class is, so the 'kind' says whose interface the container
    is asking for (see CDI_VENDOR).
    """
    return CLASS_VENDOR.get(cdiclass, CDI_VENDOR)

def class_spec_filename(cdiclass: str, stem: str, suffix: str) -> str:
    """Return the file name one class's spec is written to.

    The name is '<stem>-<class><suffix>' with the stem naming the vendor the
    spec is in: a class emitted under another vendor than CDI_VENDOR takes
    that vendor's first label, so linux.org/audio is written to
    linux-audio.json and qualcomm.com/gpu to qualcomm-gpu.json. Telling the
    two apart on disk is what keeps a 'kind' readable off the file holding it,
    and it is also what a reader of /run/cdi sees without opening anything.

    stem and suffix are --cdifilename's, so the default qualcomm.json gives
    the qualcomm-<class>.json the hardware classes have always been written
    to, and a run naming another base renames those with it. The other
    namespaces follow their own vendor instead: what the option sets is the
    Qualcomm base name, not a prefix for every spec whatever its kind.
    """
    vendor = class_vendor(cdiclass)
    if vendor != CDI_VENDOR:
        stem = vendor.partition('.')[0]
    return "%s-%s%s" % (stem, cdiclass, suffix)

def spec_annotations(vendor: str) -> dict[str, str]:
    """Return the spec-level annotations naming this file as generated.

    The keys of SPEC_ANNOTATIONS are qualified with the vendor of the spec they
    are emitted on rather than being spelled out with the prefix, so the prefix
    cannot drift from the 'kind' it is shared with (see class_vendor()).
    """
    return {vendor + "/" + key: value
            for key, value in SPEC_ANNOTATIONS.items()}

def build_cdi_spec(cdiclass: str, devices: list[dict],
                   enventries: list[str],
                   mountentries: list[dict],
                   additionalgids: list[int] | None = None) -> dict:
    """Assemble a single CDI specification dict for one device class.

    The returned structure follows the CDI spec format defined by the CNCF
    Container Device Interface project:
        https://github.com/cncf-tags/container-device-interface

    cdiclass:       device class name, suffixed onto the class's vendor for the
                    'kind' (see class_vendor())
    devices:        list of CDI device entries (as built by generate_devicenodes_cdi)
    enventries:     list of "KEY=value" strings added to containerEdits.env, as
                    generate_audio_env_entries() resolves them for this class
                    (the fastrpc classes, nothing for the rest)
    mountentries:   list of mount dicts attached to containerEdits.mounts; the
                    caller decides what belongs to this class (the DSP firmware
                    for the fastrpc classes, nothing for the rest)
    additionalgids: supplementary groups the container needs to reach the paths
                    this class passes through

    Each of the containerEdits fields is emitted only when it has something in
    it: a class that mounts nothing or grants no group leaves the field out
    rather than carrying an empty list, so the spec names what it actually does.

    Every spec carries the SPEC_ANNOTATIONS note marking the file as generated,
    since JSON has no comment to put it in.

    Access is expressed entirely through additionalGids: the container joins
    every group owning one of the class's host paths, and nothing is changed on
    the host filesystem.

    This function is pure (no filesystem access) so the generated spec can be
    validated by the upstream 'cdi' tool in the unit tests.
    """
    vendor = class_vendor(cdiclass)
    cdi = {"cdiVersion": CDI_VERSION, "kind": vendor + "/" + cdiclass}
    # Emitted here, between 'kind' and 'devices', where the Spec struct declares
    # it and where the comment would have gone (see SPEC_ANNOTATIONS).
    cdi["annotations"] = spec_annotations(vendor)
    cdi["devices"] = devices
    container_edits = {}
    # First in the map, the order the ContainerEdits struct declares it in.
    if enventries:
        container_edits["env"] = list(enventries)
    if additionalgids:
        container_edits["additionalGids"] = additionalgids
    mounts = list(mountentries or [])
    if mounts:
        container_edits["mounts"] = mounts
    if container_edits:
        cdi["containerEdits"] = container_edits
    return cdi

def is_generated_cdi(path: Path) -> bool:
    """Return whether path is a CDI spec generated by this tool."""
    try:
        with open(path, "r") as specfile:
            spec = json.load(specfile)
    except (OSError, ValueError) as err:
        logging.debug("Cannot inspect %s: %s", path, err)
        return False

    annotations = spec.get("annotations")
    if not isinstance(annotations, dict):
        return False

    return any(
        key.endswith("/generated-by")
        and value == "qualcomm-cdi-generator"
        for key, value in annotations.items()
    )

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    logging.info("Starting Qualcomm CDI generation")
    logging.info("Config: destdir=%s, cdifilename=%s, dry_run=%s",
                 args.destdir, args.cdifilename, args.dry_run)
    logging.info("Groups to grant: %s",
                 args.gid if args.gid is not None
                 else "from the host device nodes and sockets")

    if args.classes is not None:
        requested = [c.strip() for c in args.classes.split(',')]
        unknown = [c for c in requested if c not in KNOWN_CLASSES]
        if unknown:
            print("Error: unknown class(es): %s" % ", ".join(unknown),
                  file=sys.stderr)
            print("Available classes: %s" % ", ".join(KNOWN_CLASSES),
                  file=sys.stderr)
            return 1
        enabled_classes = requested
    else:
        enabled_classes = list(KNOWN_CLASSES)

    destdir = args.destdir
    cdifilename = args.cdifilename

    classgids = {}

    def resolve_gids(cdiclass: str, paths: list[str],
                     what: str = "nodes") -> dict[str, int]:
        """Resolve the group of every path of a class and record the union."""
        gidmap = node_gid_map(cdiclass, paths, args.gid, what)
        classgids.setdefault(cdiclass, set()).update(gidmap.values())
        return gidmap

    # Default empty results for all classes.
    gpu_cdi = []
    video_cdi = []
    camera_cdi = []
    audio_cdi = []
    display_cdi = []
    dmaheap_cdi = []
    cdsps_cdi = []
    adsps_cdi = []
    gdsps_cdi = []

    audiosockets = []
    displaysockets = []

    # GPU
    # Device nodes: render, card, kgsl-3d
    if 'gpu' in enabled_classes:
        rendernodes = find_devicenodes('/dev/dri/renderD*')
        cardnodes = find_devicenodes('/dev/dri/card*')
        kgslnodes = find_devicenodes('/dev/kgsl-3d*')
        gpunodes = rendernodes + cardnodes + kgslnodes
        gpugids = resolve_gids('gpu', gpunodes)
        render_cdi = generate_devicenodes_cdi('renderD', rendernodes,
                                              catchall=False, gidmap=gpugids)
        render_all_cdi = generate_devicenodes_cdi('render', rendernodes,
                                                  pernode=False, gidmap=gpugids)
        card_cdi = generate_devicenodes_cdi('card', cardnodes, catchall=False,
                                            gidmap=gpugids)
        kgsl_cdi = generate_devicenodes_cdi('kgsl-3d', kgslnodes, catchall=False,
                                            gidmap=gpugids)
        gpu_all_cdi = generate_devicenodes_cdi('gpu', gpunodes, pernode=False,
                                               gidmap=gpugids)
        gpu_cdi = (render_cdi + render_all_cdi + card_cdi + kgsl_cdi + gpu_all_cdi)

    # V4L2
    # Device nodes: video*
    if 'v4l2' in enabled_classes:
        videonodes = find_devicenodes('/dev/video*')
        videogids = resolve_gids('v4l2', videonodes)
        video_cdi = generate_devicenodes_cdi(
            'video', videonodes, gidmap=videogids)

    # Camera
    # Socket mounts: cam_server
    if 'camera' in enabled_classes:
        camerasockets = find_sockets('camera')
        resolve_gids('camera', camerasockets, "sockets")
        camera_cdi = generate_sockets_cdi('camera', camerasockets)

    # Audio
    # Socket mounts: pulse, pipewire; emits PULSE_SERVER, PIPEWIRE_REMOTE and
    # XDG_RUNTIME_DIR
    if 'audio' in enabled_classes:
        audiosockets = find_sockets('audio')
        resolve_gids('audio', audiosockets, "sockets")
        audio_cdi = generate_sockets_cdi('audio', audiosockets)

    # Display
    # Socket mounts: wayland; emits WAYLAND_DISPLAY and XDG_RUNTIME_DIR
    if 'display' in enabled_classes:
        displaysockets = find_sockets('display')
        resolve_gids('display', displaysockets, "sockets")
        display_cdi = generate_sockets_cdi('display', displaysockets)

    # DMA heap
    # Device nodes: /dev/dma_heap/*system
    if 'dmaheap' in enabled_classes:
        dmaheaps = find_devicenodes('/dev/dma_heap/*system')
        dmaheapgids = resolve_gids('dmaheap', dmaheaps)
        dmaheap_cdi = generate_devicenodes_cdi(
            'dmaheap-system', dmaheaps, gidmap=dmaheapgids)

    # FastRPC / DSP
    # Device nodes: /dev/fastrpc-{cdsp,adsp,gdsp}*, DSP firmware from /usr/lib/dsp/
    fastrpc_classes = (
        'fastrpc-cdsp',
        'fastrpc-adsp',
        'fastrpc-gdsp',
    )

    fastrpc_requested = any(
        c in enabled_classes for c in fastrpc_classes
    )

    dspmounts = {c: [] for c in fastrpc_classes}

    if fastrpc_requested:
        machinename = read_device_model()
        confdirs = find_dsp_conf_dirs()
        boardconf = read_dsp_library_path(machinename, confdirs)

        for dspclass in fastrpc_classes:
            if dspclass not in enabled_classes:
                continue

            searchdirs = get_firmware_search_dirs(
                dspclass,
                boardconf,
                machinename)

            dspmounts[dspclass] = generate_dsp_files_cdi(
                dspclass,
                searchdirs)

    if 'fastrpc-cdsp' in enabled_classes:
        cdsps = find_devicenodes('/dev/fastrpc-cdsp*')
        cdspgids = resolve_gids('fastrpc-cdsp', cdsps)
        cdsps_cdi = generate_devicenodes_cdi(
            'fastrpc-cdsp', cdsps,
            gidmap=cdspgids)

    if 'fastrpc-adsp' in enabled_classes:
        adsps = find_devicenodes('/dev/fastrpc-adsp*')
        adspgids = resolve_gids('fastrpc-adsp', adsps)
        adsps_cdi = generate_devicenodes_cdi(
            'fastrpc-adsp', adsps,
            gidmap=adspgids)

    if 'fastrpc-gdsp' in enabled_classes:
        gdsps = find_devicenodes('/dev/fastrpc-gdsp*')
        gdspgids = resolve_gids('fastrpc-gdsp', gdsps)
        gdsps_cdi = generate_devicenodes_cdi(
            'fastrpc-gdsp', gdsps,
            gidmap=gdspgids)

    cdi_sections = [
        ('gpu',          gpu_cdi,     []),
        ('v4l2',         video_cdi,   []),
        ('camera',       camera_cdi,  []),
        ('audio',        audio_cdi,   []),
        ('display',      display_cdi, []),
        ('dmaheap',      dmaheap_cdi, []),
        ('fastrpc-adsp', adsps_cdi,   dspmounts['fastrpc-adsp']),
        ('fastrpc-cdsp', cdsps_cdi,   dspmounts['fastrpc-cdsp']),
        ('fastrpc-gdsp', gdsps_cdi,   dspmounts['fastrpc-gdsp']),
    ]

    # Only the classes this run was asked for. A default run enables every
    # known class, so every section is kept and a class whose devices have
    # gone away still has its stale spec removed; a run naming --classes keeps
    # the named ones alone, which is what leaves every other CDI spec on the
    # host untouched rather than written or removed.
    cdi_sections = [section for section in cdi_sections
                    if section[0] in enabled_classes]

    dynamiccdidir = Path(destdir).joinpath('run/cdi')
    cdifilename = Path(cdifilename)
    cdifilename_stem = cdifilename.stem
    cdifilename_suffix = cdifilename.suffix

    for cdiclass, devices, classmounts in cdi_sections:
        cdipath = dynamiccdidir.joinpath(
            class_spec_filename(
                cdiclass,
                cdifilename_stem,
                cdifilename_suffix))

        if not devices:
            logging.debug("No devices found for '%s'", cdiclass)

            if not args.dry_run and cdipath.exists():
                try:
                    if is_generated_cdi(cdipath):
                        logging.info("Removing stale CDI JSON: %s", cdipath)
                        cdipath.unlink()
                    else:
                        logging.warning(
                            "Not removing %s: file was not generated by this tool",
                            cdipath)
                except OSError as err:
                    logging.warning(
                        "Failed to remove stale CDI JSON %s: %s",
                        cdipath, err)

            continue

        enventries = []

        if cdiclass == 'audio':
            enventries.extend(
                generate_audio_env_entries(audiosockets)
            )

        if cdiclass == 'display':
            enventries.extend(
                generate_display_env_entries(displaysockets)
            )

        cdi = build_cdi_spec(
            cdiclass,
            devices,
            enventries,
            classmounts,
            node_additional_gids(
                sorted(classgids.get(cdiclass, ()))))
        if args.dry_run:
            logging.info("Dry run: skipping CDI JSON write to %s", cdipath)
            continue
        Path(dynamiccdidir).mkdir(parents=True, exist_ok=True)
        tmppath = Path(str(cdipath) + ".tmp")

        try:
            with open(tmppath, "w") as cdifile:
                json.dump(cdi, cdifile, indent=JSON_INDENT)
                cdifile.write("\n")
                cdifile.flush()
                os.fsync(cdifile.fileno())

            os.replace(tmppath, cdipath)
            logging.info("Wrote CDI JSON: %s", cdipath)

            dirfd = os.open(dynamiccdidir, os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)

        except OSError:
            try:
                tmppath.unlink()
            except OSError:
                pass
            raise

    logging.info("Completed Qualcomm CDI generation")
    return 0

if __name__ == "__main__":
    sys.exit(main())
