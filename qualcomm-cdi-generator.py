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

import fnmatch
import glob
import grp
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import logging
import argparse

known_classes = ['gpu', 'v4l2', 'camera', 'audio', 'display', 'dmaheap',
                 'fastrpc-cdsp', 'fastrpc-adsp', 'fastrpc-gdsp']

# CDI specification version emitted in every generated spec file. See the CNCF
# Container Device Interface project for the format definition:
# https://github.com/cncf-tags/container-device-interface
#
# 0.7.0 is the minimum version that defines containerEdits.additionalGids, which
# the generated specs rely on to grant access to the group-owned device nodes.
# Emitting it under an older version makes the spec fail validation.
CDI_VERSION = "0.7.0"

# CDI vendor namespace prefixed to every generated 'kind' (e.g. qualcomm.com/gpu)
CDI_VENDOR = "qualcomm.com"

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
# The keys are qualified with CDI_VENDOR so they read like the 'kind' they sit
# next to and cannot collide with another tool's metadata on the same spec.
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
# location a client is pointed at in full (WAYLAND_DISPLAY=/run/wayland-0,
# PULSE_SERVER=unix:/run/pulse/native); a socket the host keeps in a per-user
# runtime directory is normalised up to it (see SOCKET_CONTAINER_DIRS).
#
# A class whose socket is absent is skipped, exactly as a class whose device
# nodes are absent is: the server is not running, so the spec would hand the
# container a path that cannot be connected to.
CLASS_SOCKETS = {
    'audio':   ['/run/pulse/native', '/run/user/*/pulse/native'],
    'camera':  ['/run/cam_server/le_camera_socket'],
    'display': ['/run/wayland-*', '/run/user/*/wayland-*'],
}

# Suffixes a class's socket glob can match that are not sockets and must not be
# mounted. A Wayland compositor keeps a '<socket>.lock' lock file next to every
# socket it serves, so '/run/user/*/wayland-*' matches /run/user/1000/wayland-1
# and /run/user/1000/wayland-1.lock alike.
#
# Mounting the lock file would hand the container a path no client connects to,
# and give it the host compositor's lock to take. These are dropped before a
# pattern counts as matched, so a runtime directory holding nothing but a
# leftover lock file falls through to the next pattern instead of being taken
# for a running compositor.
SOCKET_IGNORE_SUFFIXES = ('.lock',)

# Classes whose per-user sockets are taken from the regular logins alone, and
# the lowest uid such a login is given. A class listed here has the sockets of
# every system account's session dropped by select_login_sockets().
#
# Every login's sockets are passed through, not one chosen session's: a target
# with two users logged in at once has two compositors and two sound servers
# worth reaching, and the container is handed both rather than whichever one
# this generator preferred.
#
# They do collide on the way in, both classes and for the same reason -- the
# remapping to /run drops the uid that told the sessions apart. A compositor
# calls its first socket wayland-0 whatever another compositor on the host has
# called its own, so two sessions give /run/user/1000/wayland-0 and
# /run/user/1001/wayland-0; a sound server's socket is the constant 'native',
# so two sessions give /run/user/1000/pulse/native and
# /run/user/1001/pulse/native. Either way both remap onto one container path
# (see SOCKET_CONTAINER_DIRS) and the later mount point is suffixed (see
# SOCKET_COLLISION_SUFFIXED), so every server stays reachable and none is
# shadowed.
#
# What is dropped is a session belonging to a system account, a display manager
# greeter being the one most targets have: its compositor is not what a
# container is meant to draw on, nor its sound server what a container is meant
# to play through, and passing it through would spend the unsuffixed name on it
# if it sorted first. SESSION_UID_MIN is the floor, ranked rather than pinned,
# so a target whose users run as something other than 1000 is handled the same
# way. A host running nothing but system sessions keeps them after all, those
# servers being the only ones there are.
#
# The camera class is not listed: its server has one system-wide socket and no
# sessions to tell apart.
SOCKET_LOGIN_SESSION_CLASSES = ('audio', 'display')
SESSION_UID_MIN = 1000

# Mount options for the sockets above. Unlike the read-only devicetree model
# node these are read-write: a unix socket is useless to a client that cannot
# write to it.
SOCKET_MOUNT_OPTIONS = ["nosuid", "noexec", "rw", "bind"]

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
# generator asks first: the model read off the devicetree picks the entry, and
# DSP_LIBRARY_PATH resolved against the directory holding the conf.d gives the
# host firmware directory (see read_dsp_library_path()). No SoC id table and no
# per-board path compiled in here can go stale against the image the way one
# keyed off /sys/devices/soc0/soc_id did.
#
# The conf.d is bind-mounted into the container as well (see
# generate_dsp_conf_cdi()), so whatever resolves DSP_LIBRARY_PATH in there
# reads the same file the generator read.
#
# Both locations are searched, in this order: the qcom one is where the
# qualcomm-linux images install it and the hexagon-dsp one where the package of
# that name does. Whichever of them the host turns out to have, the container
# is given it at DSP_CONF_CONTAINER_DIR -- one path to read the configuration
# from, as there is one path to read the firmware from (see
# DSP_FIRMWARE_CONTAINER_DIR), so nothing in the container has to know which
# layout the host used.
DSP_CONF_DIRS = ('/usr/share/qcom/conf.d', '/usr/share/hexagon-dsp/conf.d')
DSP_CONF_CONTAINER_DIR = '/usr/share/hexagon-dsp/conf.d'
DSP_CONF_GLOBS = ('*.yaml', '*.yml')
DSP_CONF_MACHINES_KEY = 'machines'
DSP_LIBRARY_PATH_KEY = 'DSP_LIBRARY_PATH'

# Mount options for the conf.d directory. Read-only: it is the board's
# configuration to describe the firmware, not the container's to rewrite.
DSP_CONF_MOUNT_OPTIONS = ["nosuid", "ro", "bind"]

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

# Directory the DSP firmware files appear under inside the container.
#
# This is where they have to appear and the only place they appear: the host
# directory they came from is board specific and is not mounted, so a file
# keeps its per-DSP sub-path and nothing else of the path it was found at --
# <board dir>/cdsp/fastrpc_shell_3 is handed over as
# /usr/share/hexagon-dsp/cdsp/fastrpc_shell_3.
DSP_FIRMWARE_CONTAINER_DIR = '/usr/share/hexagon-dsp'

# DSP firmware files mounted individually into DSP_FIRMWARE_CONTAINER_DIR, as
# globs relative to a host DSP firmware directory, per device class. Their
# sub-path is preserved, so cdsp/fastrpc_shell_unsigned_3 lands on
# /usr/share/hexagon-dsp/cdsp/fastrpc_shell_unsigned_3 and a differently
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
DSP_FIRMWARE_FILES = {
    'fastrpc-cdsp': [
        'cdsp*/fastrpc_shell_*',
        'cdsp*/libc++.so.1',
        'cdsp*/libc++abi.so.1',
    ],
    'fastrpc-adsp': [
        'adsp*/fastrpc_shell_*',
        'adsp*/libc++.so.1',
        'adsp*/libc++abi.so.1',
    ],
    'fastrpc-gdsp': [
        'gdsp*/fastrpc_shell_*',
        'gdsp*/libc++.so.1',
        'gdsp*/libc++abi.so.1',
    ],
}

# Mount options for the files above, which are the only DSP firmware a spec
# carries: the directories holding them on the host are not mounted at all, so
# a container is given the files its FastRPC client opens and no more.
#
# Read-write, as the hand-maintained specs these replace mount them.
DSP_FILE_MOUNT_OPTIONS = ["nosuid", "rw", "bind"]

# The devicetree model node, which holds the board name, and where the fastrpc
# specs hand it to the container.
#
# The board name is what the FastRPC client resolves its firmware paths
# against. On a host it reads the string from the devicetree node directly; in
# a container that read is not dependable, because runtimes filter /sys. The
# node is bind-mounted under /run instead, which a container reaches, so the
# string is there to be read whatever /sys serves.
#
# Only the fastrpc classes carry the mount: nothing in the other classes
# resolves a board name.
DEVICE_MODEL_HOST_PATH = '/sys/firmware/devicetree/base/model'
DEVICE_MODEL_CONTAINER_PATH = '/run/device-model'

# Mount options for the node above. Read-only -- the container has no business
# writing a board name back -- and a recursive bind, so the mount resolves the
# sysfs node itself rather than whatever mount point it sits under.
DEVICE_MODEL_MOUNT_OPTIONS = ["rbind", "ro"]

# Environment variable carrying the board name into the container, and the
# classes whose specs set it.
#
# The FastRPC stack resolves its Hexagon binary paths against the board name
# and takes it from this variable when it is set, reading the devicetree node
# itself otherwise. Both are handed over: the variable saves the client that
# read, and the mounted node is there for whatever reads the name directly (see
# DEVICE_MODEL_HOST_PATH), so neither way in depends on the other having
# worked.
#
# Only the fastrpc classes set it, the same ones that carry the mount, and for
# the same reason: resolving a path against the board name is what their
# FastRPC stack does and what no other class does. A variable in the other
# specs would be exported into every container that asks for a GPU or a camera,
# which is a reach into the image's environment that nothing in those classes
# needs.
#
# Unlike the mount, which names the node and leaves the reading to the
# container, this carries the string read at generation time. That is as
# current as the file holding it: the service regenerates every spec from
# scratch on each boot, so the name cannot outlive the board it was read from.
MACHINE_NAME_ENV = 'MACHINE_NAME'
MACHINE_NAME_CLASSES = ('fastrpc-cdsp', 'fastrpc-adsp', 'fastrpc-gdsp')

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
# /run, the path a client is pointed at: a Wayland client with an absolute
# WAYLAND_DISPLAY=/run/wayland-0 connects to exactly that socket, a PulseAudio
# client with PULSE_SERVER=unix:/run/pulse/native likewise. Nothing in the
# container looks under /run/user/<uid>, and that directory cannot be handed
# over wholesale either, since mounting it on /run would shadow the
# container's own /run.
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
# with PULSE_SERVER=unix:/run/pulse/native-1 or
# WAYLAND_DISPLAY=/run/wayland-0-1 without the host uid ever appearing inside
# the container.
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
SOCKET_COLLISION_SUFFIXED = [
    '/run/pulse/native',
    '/run/user/*/pulse/native',
    '/run/wayland-*',
    '/run/user/*/wayland-*',
]

# Groups the container is put into so it can reach the host resources a class
# passes through. The spec carries them as containerEdits.additionalGids and
# nothing on the host filesystem is touched.
#
# The group is taken from the host path itself (see node_gid_map()), because
# that is the group the driver and its udev rules actually hand the device to,
# and it differs per board: a render node may be 'render' on one target and the
# container user's primary group on another. It can also differ *within* a
# class on one board, so the group is resolved per path and the union granted.
#
# The fallback only applies to a path with no group of its own (it could not be
# stat()ed, or it is owned by group root), and is resolved in three steps, see
# class_fallback_gid(): the group CLASS_GROUP names for the class, then the
# number CLASS_GID gives it, then DEFAULT_GID.
#
# CLASS_GROUP names the group rather than numbering it because these groups are
# named alike across images and numbered differently. The same board under the
# two images it is booted with:
#
#                     qualcomm-linux   Debian/Ubuntu
#     video                       44              44
#     audio                       29              29
#     render                     993             993
#     dmaheap                    992             988
#     fastrpc                    999             126
#
# A compiled-in number is therefore wrong on one image or the other, while
# getgrnam() gets it right on both and on an image that renumbers them again.
# camera has no group of its own in either (the camera server's socket carries
# whatever group the server runs as), so it is left to its number.
#
# CLASS_GID is the number to use when the name is not in the group database at
# all -- an image that ships the nodes without the group, where there is no gid
# to look up and the node's own group is what a working target would have given
# us anyway.
#
# DEFAULT_GID is the last resort under both, for a class CLASS_GID has no entry
# for at all. It is not granted on top of what the nodes are owned by: a spec
# names the groups that own the paths it hands over and nothing else, so a
# container is put into the groups it needs to reach them and into no group
# chosen for it. 1000 would be the wrong one to choose anyway -- it is the
# container user's primary group under qualcomm-linux (user 'weston') but not
# under every image, Debian/Ubuntu having user 'ubuntu' at uid 1000 with
# primary gid 1001.
DEFAULT_GID = 1000
CLASS_GROUP = {
    'gpu':          'render',
    'v4l2':         'video',
    'audio':        'audio',
    'display':      'wayland',
    'dmaheap':      'dmaheap',
    'fastrpc-cdsp': 'fastrpc',
    'fastrpc-adsp': 'fastrpc',
    'fastrpc-gdsp': 'fastrpc',
}
CLASS_GID = {
    'gpu':          1000,
    'v4l2':         44,
    'camera':       1000,
    'audio':        1000,
    'display':      1000,
    'dmaheap':      1000,
    'fastrpc-cdsp': 1000,
    'fastrpc-adsp': 1000,
    'fastrpc-gdsp': 1000,
}

def class_fallback_gid(cdiclass: str) -> int:
    """Return the gid to use for a path of this class with no group of its own.

    The group CLASS_GROUP names for the class is looked up on this host first,
    so the fallback is the gid the image really gave that group rather than the
    number some other image gave it. A class with no name, or a name the group
    database does not hold, drops to its CLASS_GID number and then DEFAULT_GID.

    The lookup is by name on purpose: 'dmaheap' is 992 under qualcomm-linux and
    988 under Debian/Ubuntu, 'fastrpc' 999 and 126, so no compiled-in number is
    right for both while the name is (see CLASS_GROUP).
    """
    groupname = CLASS_GROUP.get(cdiclass)
    if groupname:
        try:
            return grp.getgrnam(groupname).gr_gid
        except KeyError:
            logging.debug("No '%s' group on this host for '%s'; using the "
                          "built-in gid", groupname, cdiclass)
    return CLASS_GID.get(cdiclass, DEFAULT_GID)

def node_gid_map(cdiclass: str, nodes: list[str] | None = None,
                 gid: int | None = None, what: str = "nodes") -> dict[str, int]:
    """Map each device node of one class to the group to grant for it.

    An explicit --gid always wins, for every node. Otherwise each node's group
    is read from the node itself, so the spec names the group that actually owns
    the device on this board rather than a compiled-in guess.

    The group is resolved per node because one class can span several groups on
    a single board: a target may expose /dev/video0 in 'video' while a node
    alongside it belongs to a vendor group. Collapsing those to one gid would
    leave every node outside the winning group unopenable, so each node keeps
    its own and build_cdi_spec() grants the union (see node_additional_gids).

    The per-class fallback stands in for any node with no group of its own: one
    that disappeared between the glob and the stat, or one owned by group root.
    A root-owned node is not honoured because the runtime drops a 0
    additionalGids entry, so a 0 here would grant an unprivileged container
    nothing. The fallback is itself read off this host where it can be, by the
    group name the class uses rather than a compiled-in number (see
    class_fallback_gid()).

    what names the paths in the log lines; the socket-backed classes pass
    'sockets' since they have no device node to speak of.
    """
    fallback = class_fallback_gid(cdiclass)
    gidmap = {}
    for node in sorted(nodes or []):
        if gid is not None:
            gidmap[node] = gid
            continue
        try:
            nodegid = os.stat(node).st_gid
        except OSError as err:
            logging.debug("Cannot stat %s for its group, using gid %d: %s",
                          node, fallback, err)
            nodegid = fallback
        if not nodegid:
            logging.debug("%s is owned by group root, using gid %d instead",
                          node, fallback)
            nodegid = fallback
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

def node_additional_gids(cdiclass: str,
                         gids: int | list[int] | None = None) -> list[int]:
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

    gid 0 is dropped: the runtime skips a 0 entry when it builds the OCI spec,
    so emitting it would only add a no-op to every generated file.
    """
    if gids is None:
        gids = [class_fallback_gid(cdiclass)]
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
    parser.add_argument("-c", "--cdifilename", default="qualcomm.json", help="CDI JSON filename base; the device class is inserted before the extension, e.g. qualcomm.json -> qualcomm-gpu.json (default: %(default)s)")
    parser.add_argument("-C", "--classes", default=None, help="Comma-separated list of CDI classes to generate (default: all). Available: %s" % ", ".join(known_classes))
    parser.add_argument("-g", "--gid", type=int, default=None, metavar="GID", help="gid to grant via additionalGids, replacing both the group read from the host device nodes and sockets and the default gid the device-node classes are granted")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Parse and probe devices but do not write any files")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv)")
    return parser.parse_args()

def find_devicenodes(deviceglob: str) -> list[str]:
    logging.debug("Globbing device nodes: %s", deviceglob)
    files = glob.glob(deviceglob)
    logging.info("Found %d nodes for pattern %s", len(files), deviceglob)
    logging.debug("Nodes for %s: %s", deviceglob, files)
    return files

def find_sockets(cdiclass: str) -> list[str]:
    """Return the host sockets to mount for one class, keeping only those present.

    The class's patterns (see CLASS_SOCKETS) are globbed in order and the first
    one that matches anything wins; the later patterns are the fallbacks for a
    target that puts the socket elsewhere. A system-wide socket under /run is
    listed first and the per-user runtime directory second, so a system server
    is preferred and a session one is still found. Stopping at the first match
    keeps a single compositor or sound server from being mounted twice under two
    different paths.

    All matches of the winning pattern are kept, since one pattern can legitimately
    name several sockets (a compositor exposing wayland-0 and wayland-1, or one
    per logged-in user), except for two things. A SOCKET_IGNORE_SUFFIXES path is
    not a socket at all and is dropped before the pattern is considered to have
    matched anything, and a SOCKET_LOGIN_SESSION_CLASSES class drops the
    sockets of a system account's session, keeping every regular login's.

    No match from any pattern means the host-side server is not running, so there
    is nothing for the container to connect to and the class is skipped. This is
    the socket equivalent of an empty device node glob.
    """
    for pattern in CLASS_SOCKETS.get(cdiclass, []):
        matches = []
        for path in sorted(glob.glob(pattern)):
            if path.endswith(SOCKET_IGNORE_SUFFIXES):
                logging.debug("Not a %s socket, ignoring %s", cdiclass, path)
                continue
            matches.append(path)
        if matches:
            if cdiclass in SOCKET_LOGIN_SESSION_CLASSES:
                matches = select_login_sockets(cdiclass, matches)
            for path in matches:
                logging.info("Found %s socket: %s", cdiclass, path)
            return matches
        logging.debug("No %s socket matching %s", cdiclass, pattern)
    return []

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

def select_login_sockets(cdiclass: str, sockets: list[str]) -> list[str]:
    """Drop the sockets a class must not take from a system account's session.

    Every regular login's sockets are kept, so a host with two users logged in
    at once hands the container both sessions' servers. Two sessions then do
    ask for one container path, and that is settled by suffixing the later
    mount point rather than by choosing a session here (see
    SOCKET_COLLISION_SUFFIXED).

    A session whose runtime directory belongs to a uid below SESSION_UID_MIN is
    a system account's -- a display manager greeter above all -- and is left
    out: what it serves is not what a container is meant to draw on or play
    through. A socket with no runtime uid belongs to no session, being served
    system-wide, and is kept.

    A host running nothing but system sessions keeps them, those servers being
    the only ones there are; the filter is what a target with a login does
    instead of handing the greeter's name out. Every runtime directory dropped
    is logged: the sockets are there and were deliberately not passed on, which
    is worth saying for a target where the uids are not what this expects.

    The sockets are returned lowest session uid first, so which one keeps the
    unsuffixed container path does not depend on the order they were found in
    (see socket_session_order()).
    """
    bydir: dict[str, list[str]] = {}
    for socket in sockets:
        bydir.setdefault(str(PurePosixPath(socket).parent), []).append(socket)

    def is_login(runtimedir: str) -> bool:
        sessionuid = socket_runtime_uid(bydir[runtimedir][0])
        return sessionuid is None or sessionuid >= SESSION_UID_MIN

    logins = [runtimedir for runtimedir in bydir if is_login(runtimedir)]
    if not logins:
        logging.debug("No %s socket of a login session, keeping the %d system "
                      "session(s) found", cdiclass, len(bydir))
        logins = list(bydir)
    for runtimedir, dirsockets in bydir.items():
        if runtimedir not in logins:
            logging.info("Skipping %d %s socket(s) in %s: uid %d is a system "
                         "account, not a login of uid %d or above",
                         len(dirsockets), cdiclass, runtimedir,
                         socket_runtime_uid(dirsockets[0]), SESSION_UID_MIN)
    return sorted((socket for runtimedir in logins
                   for socket in bydir[runtimedir]), key=socket_session_order)

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

    The file is YAML, but only the one shape this reads matters: a 'machines'
    mapping of devicetree model to a mapping of keys, DSP_LIBRARY_PATH among
    them. It is parsed here rather than with a YAML library because the
    generator runs on a target with nothing but the standard library, and the
    service that runs it must not depend on a package the image may not carry.

    Indentation is what the shape is read from: the model names are the keys
    one level under 'machines' and their settings the level below that. A line
    that is blank, a comment, or outside the 'machines' mapping is skipped, so
    the SPDX header and the schema annotation these files carry cost nothing.
    Quotes around a name or a value are dropped.
    """
    machines: dict[str, dict[str, str]] = {}
    try:
        with open(conffile, "r") as conf:
            lines = conf.read().splitlines()
    except OSError as err:
        logging.info("Cannot read the DSP configuration %s: %s", conffile, err)
        return machines

    def unquote(text: str) -> str:
        text = text.strip()
        if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
            return text[1:-1]
        return text

    inmachines = False
    machine = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            # A top-level key: 'machines' opens the mapping this reads and
            # anything else closes it, so other sections are passed over.
            inmachines = stripped.rstrip(':') == DSP_CONF_MACHINES_KEY
            machine = None
            continue
        if not inmachines:
            continue
        key, _, value = stripped.partition(':')
        if not value.strip():
            # A mapping key with nothing after the colon: a model name.
            machine = unquote(key)
            machines.setdefault(machine, {})
        elif machine is not None:
            machines[machine][unquote(key)] = unquote(value)
    return machines

def read_dsp_library_path(machinename: str | None,
                          confdirs: list[str]) -> tuple[str, str] | None:
    """Return (conf.d directory, DSP_LIBRARY_PATH) for this board, or None.

    Every configuration file in confdirs is read in turn and the first one
    naming this board's devicetree model wins, which is what resolves the
    firmware directory the image itself says the board loads.

    The conf.d directory is returned alongside the path because both follow
    from it: the directory holding it is what the relative path resolves
    against, so the qcom conf.d gives /usr/share/qcom/<DSP_LIBRARY_PATH> and
    the hexagon-dsp one /usr/share/hexagon-dsp/<DSP_LIBRARY_PATH>, and it is
    also the one the container is given, this being the file that named its
    board (see dsp_conf_mount_dir()).

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

def dsp_conf_mount_dir(boardconf: tuple[str, str] | None,
                       confdirs: list[str]) -> str | None:
    """Return the host conf.d to hand to the container, or None for no mount.

    The one that named this board is preferred over the one that happens to
    come first: with both locations installed, the container has to be given
    the file its own board is in, or DSP_LIBRARY_PATH resolves to nothing in
    there. Only one can be handed over, both being presented at the single
    DSP_CONF_CONTAINER_DIR path.

    A host whose configuration names no entry for this board still gets its
    first conf.d passed through: there is nothing to choose between them, and
    the container is no worse off reading what the host reads.
    """
    if boardconf:
        return boardconf[0]
    return confdirs[0] if confdirs else None

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
    at (see find_dsp_firmware_dirs()). machinename is carried for that warning.
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

def find_dsp_firmware_files(cdiclass: str, dspdir: str) -> list[str]:
    """Return one class's firmware files inside one host firmware directory.

    The DSP_FIRMWARE_FILES globs of cdiclass are resolved against dspdir, so
    the result is the firmware of that class's own DSP and nothing else. An
    empty list means this directory holds no firmware for the class: another
    board's directory, or this board's for a DSP it does not have.

    This answers both questions asked about a candidate directory -- whether it
    is the one to resolve the class from, and which per-DSP sub-directories to
    mount out of it -- from the files themselves rather than from a second
    table of directory names to keep in step.
    """
    found = []
    for fileglob in DSP_FIRMWARE_FILES.get(cdiclass, []):
        pattern = str(PurePosixPath(dspdir, fileglob))
        logging.debug("Globbing DSP files: %s", pattern)
        found += sorted(glob.glob(pattern))
    return found

def find_dsp_firmware_dirs(cdiclass: str, boarddirs: list[str]) -> list[str]:
    """Return the host DSP firmware directory to resolve one class from.

    boarddirs are what this board answered with (see
    dsp_firmware_board_dirs()), tried in order; the first holding this class's
    firmware wins. There is no guessing past them: a board that answered with
    nothing, or whose directory holds nothing for this class, gets no firmware
    mounts for it.

    That is deliberate. The alternative is looking for a firmware directory
    nothing pointed at, which on an image built for a family of targets means
    another board's -- and firmware built for a different SoC is worse than no
    firmware, being exactly the mismatch mounting the host's copies avoids.

    An empty list therefore means this host said nothing about where that DSP's
    firmware is, and its spec carries the device nodes and the board name
    without it.
    """
    for boarddir in boarddirs:
        if find_dsp_firmware_files(cdiclass, boarddir):
            logging.info("Resolving '%s' firmware from %s", cdiclass, boarddir)
            return [boarddir]
        logging.debug("The board directory %s holds no '%s' firmware",
                      boarddir, cdiclass)
    logging.info("No '%s' firmware for this board%s", cdiclass,
                 " in " + ", ".join(boarddirs) if boarddirs else "")
    return []

def generate_dsp_conf_cdi(confdir: str | None) -> list[dict]:
    """Build the mount entry for the DSP configuration directory.

    The host's conf.d is handed over so whatever resolves DSP_LIBRARY_PATH
    inside the container reads the file the generator read rather than a copy
    the image shipped for another board. It is the one directory a fastrpc spec
    mounts, the firmware itself reaching the container as individual files (see
    generate_dsp_files_cdi()), and it is read-only.

    It is presented at DSP_CONF_CONTAINER_DIR whichever of DSP_CONF_DIRS the
    host keeps it in, so a container resolves one path for the configuration as
    it does for the firmware and needs to know nothing about the host's layout.
    Which host directory that is, where both exist, is dsp_conf_mount_dir()'s
    to decide -- only one can be presented at a single path.

    No conf.d means no mount: an image carrying no configuration has nothing to
    hand over, and its firmware was resolved another way.
    """
    if not confdir:
        return []
    logging.info("Mounting DSP configuration directory %s into the container "
                 "at %s", confdir, DSP_CONF_CONTAINER_DIR)
    return [{"hostPath": confdir,
             "containerPath": DSP_CONF_CONTAINER_DIR,
             "options": list(DSP_CONF_MOUNT_OPTIONS)}]

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
    PULSE_SERVER=unix:/run/pulse/native-1 or WAYLAND_DISPLAY=/run/wayland-0-1.

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

    Each directory of dspdirs is searched for the DSP_FIRMWARE_FILES globs of
    cdiclass and every match is mounted into DSP_FIRMWARE_CONTAINER_DIR under
    the same per-DSP sub-path, so the FastRPC stack in the container finds the
    Hexagon shell and its C++ runtime at one fixed location whatever
    board-specific directory the host kept them in.

    These are the only firmware mounts a spec carries: the host directory they
    were found in is not mounted, so what reaches the container is the files
    its client opens and not the board directory around them.

    Only the globs of cdiclass are searched, so each fastrpc class carries the
    firmware of its own DSP: the cdsp*/ files reach the fastrpc-cdsp spec, the
    adsp*/ files the fastrpc-adsp one and the gdsp*/ files the fastrpc-gdsp
    one. A class with no entry in DSP_FIRMWARE_FILES mounts no file
    individually.

    dspdirs is searched in order and the first directory providing a file
    wins, since two mounts on one container path would be a conflicting spec.
    There is normally one directory to search: a board that answered with a
    firmware directory is resolved to that one alone (see
    find_dsp_firmware_dirs()).
    """
    mounts = {}
    for dspdir in dspdirs:
        for hostpath in find_dsp_firmware_files(cdiclass, dspdir):
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
                "options": list(DSP_FILE_MOUNT_OPTIONS),
            }
    return list(mounts.values())

def read_device_model() -> str | None:
    """Return the board name the host devicetree model node holds.

    This is the string the fastrpc specs carry as MACHINE_NAME, and the board
    the generator believes it is on -- the first thing to check when a target
    resolves the wrong firmware -- so it is logged as it is read whether or not
    a spec ends up using it.

    None means there is no name to pass on, from a host with no model node at
    all or one whose node could not be read, and then no spec sets the
    variable. The mount is decided separately (see generate_device_model_cdi()),
    so a node that is there but unreadable from here is still handed to the
    container to read for itself.

    The node is NUL-terminated and the terminator is dropped, along with any
    whitespace around the name: the string becomes the value of an environment
    variable, which cannot carry a NUL at all, and a trailing newline would be
    part of every board name a client compares it against.
    """
    try:
        with open(DEVICE_MODEL_HOST_PATH, "r") as modelnode:
            machinename = modelnode.read().replace('\u0000', '').strip()
    except OSError as err:
        logging.info("Cannot read the board name from %s, setting no %s: %s",
                     DEVICE_MODEL_HOST_PATH, MACHINE_NAME_ENV, err)
        return None
    if not machinename:
        logging.info("The devicetree model at %s holds no board name, setting "
                     "no %s", DEVICE_MODEL_HOST_PATH, MACHINE_NAME_ENV)
        return None
    logging.info("Detected %s from devicetree", machinename)
    return machinename

def generate_enventries_cdi(cdiclass: str,
                            machinename: str | None) -> list[str]:
    """Build the containerEdits.env entries one class carries.

    MACHINE_NAME is the only variable the generated specs set, and only the
    MACHINE_NAME_CLASSES set it: the board name is what their FastRPC stack
    resolves its Hexagon binary paths against, and it is handed over next to
    the firmware those paths lead to.

    Every other class gets no env entries at all. What they pass through is a
    path -- a device node or a socket -- and the variable naming it is the
    image's and the docker run command line's to set, not a generated file's.

    A host with no board name to pass on sets nothing either, rather than
    exporting an empty MACHINE_NAME for a client to resolve its firmware paths
    against. The list return is what makes both the empty case, which
    build_cdi_spec() then leaves out of the spec entirely.
    """
    if cdiclass not in MACHINE_NAME_CLASSES or machinename is None:
        return []
    logging.info("Setting %s=%s in the '%s' spec", MACHINE_NAME_ENV,
                 machinename, cdiclass)
    return ["%s=%s" % (MACHINE_NAME_ENV, machinename)]

def generate_device_model_cdi() -> list[dict]:
    """Build the mount handing the devicetree model node to the container.

    The node holds the board name the FastRPC client resolves its firmware
    paths against, and it is mounted under /run rather than left where it is
    because a container cannot count on reaching /sys (see
    DEVICE_MODEL_HOST_PATH). The MACHINE_NAME the specs set from the same node
    is not a replacement for it: the mount is what lets anything in the
    container read the name for itself, whatever its environment holds.

    The mount is on the node being there and not on its contents having been
    read, so a container still gets whatever the node holds on a host where
    read_device_model() came away with nothing.

    A host with no model node gets no mount, and the fastrpc specs are still
    written: the DSP nodes and their firmware do not depend on the board name
    being readable. The list return is what makes that the empty case, so the
    caller can concatenate it either way.
    """
    if not glob.glob(DEVICE_MODEL_HOST_PATH):
        logging.info("No devicetree model at %s; mounting no board name",
                     DEVICE_MODEL_HOST_PATH)
        return []
    logging.info("Mounting the devicetree model %s into the container at %s",
                 DEVICE_MODEL_HOST_PATH, DEVICE_MODEL_CONTAINER_PATH)
    return [{
        "hostPath": DEVICE_MODEL_HOST_PATH,
        "containerPath": DEVICE_MODEL_CONTAINER_PATH,
        "options": list(DEVICE_MODEL_MOUNT_OPTIONS),
    }]

def generate_sockets_cdi(nickname: str, sockets: list[str]) -> list[dict]:
    """Build the CDI device entries for a socket-only class.

    The camera, audio and display classes pass through no device node at all,
    so their device entry carries the socket mount rather than a deviceNodes
    list. The container asks for qualcomm.com/audio=audio and gets the socket;
    without a device entry the class could not be requested on the command line
    at all, since a runtime only exposes named devices.

    A single device named after the class is emitted, plus the ':all' catch-all
    the other classes provide, so the two spellings behave the same everywhere.
    """
    if not sockets:
        logging.debug("No sockets for '%s'; no CDI entries generated", nickname)
        return []
    logging.info("Generating CDI entries for '%s' with %d socket(s)",
                 nickname, len(sockets))
    mounts = generate_mounts_cdi(sockets)
    return [{"name": nickname, "containerEdits": {"mounts": mounts}},
            {"name": nickname + ":all", "containerEdits": {"mounts": mounts}}]

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
        logging.info("Generating CDI entries for '%s' with %d node(s)", nickname, len(filesglob) if filesglob else 0)
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
            device_entrys = { "name": nickname+":all", "containerEdits": device_pathlist }
            logging.debug("CDI catch-all entry: %s", device_entrys)
            devicenodelist.append(device_entrys)
    else:
        devicenodelist = []
        logging.debug("No nodes found for '%s'; no CDI entries generated", nickname)
    return devicenodelist

def get_devicenode_index(nodename: str) -> int | None:
    # Extract a trailing integer from the node name (e.g. 128 from 'renderD128')
    nodeindex = re.search(r'\d+$', nodename)
    return int(nodeindex.group()) if nodeindex else None

def spec_annotations() -> dict[str, str]:
    """Return the spec-level annotations naming this file as generated.

    The keys of SPEC_ANNOTATIONS are qualified with the CDI vendor here rather
    than being spelled out with the prefix, so the prefix cannot drift from
    CDI_VENDOR and the 'kind' it is shared with.
    """
    return {CDI_VENDOR + "/" + key: value
            for key, value in SPEC_ANNOTATIONS.items()}

def build_cdi_spec(cdiclass: str, devices: list[dict],
                   enventries: list[str],
                   mountentries: list[dict | None],
                   additionalgids: list[int] | None = None) -> dict:
    """Assemble a single CDI specification dict for one device class.

    The returned structure follows the CDI spec format defined by the CNCF
    Container Device Interface project:
        https://github.com/cncf-tags/container-device-interface

    cdiclass:       device class name, suffixed onto CDI_VENDOR for the 'kind'
    devices:        list of CDI device entries (as built by generate_devicenodes_cdi)
    enventries:     list of "KEY=value" strings added to containerEdits.env, as
                    generate_enventries_cdi() resolves them for this class (the
                    board name for the fastrpc classes, nothing for the rest)
    mountentries:   list of mount dicts attached to containerEdits.mounts; the
                    caller decides what belongs to this class (the DSP firmware
                    and the board name for the fastrpc classes, nothing for the
                    rest)
    additionalgids: supplementary groups the container needs to reach the paths
                    this class passes through

    MACHINE_NAME is the only variable a spec sets, and only the fastrpc specs
    set it, that being the board name their FastRPC stack resolves its Hexagon
    binary paths against. Everything else a class passes through is a path -- a
    device node, a socket or a file -- whose variable is the image's and the
    command line's to set, so no other class names one.

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
    cdi = {"cdiVersion": CDI_VERSION, "kind": CDI_VENDOR + "/" + cdiclass}
    # Emitted here, between 'kind' and 'devices', where the Spec struct declares
    # it and where the comment would have gone (see SPEC_ANNOTATIONS).
    cdi["annotations"] = spec_annotations()
    cdi["devices"] = devices
    container_edits = {}
    # First in the map, the order the ContainerEdits struct declares it in.
    if enventries:
        container_edits["env"] = list(enventries)
    if additionalgids:
        container_edits["additionalGids"] = additionalgids
    # filter(None, ...) strips any None placeholders left in a pre-allocated list
    mounts = list(filter(None, mountentries or []))
    if mounts:
        container_edits["mounts"] = mounts
    cdi["containerEdits"] = container_edits
    return cdi

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    logging.info("Starting Qualcomm CDI generation")
    logging.info("Config: destdir=%s, cdifilename=%s, dry_run=%s", args.destdir, args.cdifilename, args.dry_run)
    logging.info("Groups to grant: %s",
                 args.gid if args.gid is not None
                 else "from the host device nodes and sockets")

    if args.classes is not None:
        requested = [c.strip() for c in args.classes.split(',')]
        unknown = [c for c in requested if c not in known_classes]
        if unknown:
            print("Error: unknown class(es): %s" % ", ".join(unknown), file=sys.stderr)
            print("Available classes: %s" % ", ".join(known_classes), file=sys.stderr)
            return 1
        enabled_classes = requested
    else:
        enabled_classes = known_classes

    # Use CLI-configured values
    destdir = args.destdir
    cdifilename = args.cdifilename

    # Groups to grant the container, resolved per path from the host paths just
    # probed, so every node and socket is reachable under the group it is
    # actually handed to on this board unless it is overridden on the CLI.
    # classgids collects the groups each class ended up using, which become
    # that spec's additionalGids.
    classgids = {}

    def resolve_gids(cdiclass: str, paths: list[str],
                     what: str = "nodes") -> dict[str, int]:
        """Resolve the group of every path of a class and record the union."""
        gidmap = node_gid_map(cdiclass, paths, args.gid, what)
        classgids.setdefault(cdiclass, set()).update(gidmap.values())
        return gidmap

    # Find the GPU nodes. The render node is the one a container renders
    # through, but a full GPU stack also needs the DRM card node, and on a
    # Qualcomm target the Adreno KGSL node when the kernel exposes one (older
    # downstream kernels use /dev/kgsl-3d0 rather than going through DRM).
    rendernodes = find_devicenodes('/dev/dri/renderD*')
    cardnodes = find_devicenodes('/dev/dri/card*')
    kgslnodes = find_devicenodes('/dev/kgsl-3d*')
    gpunodes = rendernodes + cardnodes + kgslnodes
    # One gid resolution for the whole class: the nodes often differ (a render
    # node in 'render' next to a card node in 'video'), and additionalGids has
    # to carry every one of those groups.
    gpugids = resolve_gids('gpu', gpunodes)
    # The render nodes are named after the renderD* nodes themselves, but their
    # catch-all is 'render:all', named after the node class rather than the
    # renderD prefix one node happens to spell it with. The card and KGSL nodes
    # are added alongside under their own names, plus a single 'gpu:all' that
    # hands over the whole GPU stack at once. Every call is handed the one gid
    # map of the class, so a node names the same group wherever it appears.
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
    gpu_cdi = render_cdi + render_all_cdi + card_cdi + kgsl_cdi + gpu_all_cdi

    # Find all videonoodes and generate entries
    videonodes = find_devicenodes('/dev/video*')
    videogids = resolve_gids('v4l2', videonodes)
    video_cdi = generate_devicenodes_cdi('video', videonodes, gidmap=videogids)

    # Socket-only classes. There is no device node to pass through: the
    # container reaches the host camera server, audio server and compositor
    # over a unix socket, so the bind-mounted socket is the whole device and it
    # rides on the device entry itself.
    camerasockets = find_sockets('camera')
    resolve_gids('camera', camerasockets, "sockets")
    camera_cdi = generate_sockets_cdi('camera', camerasockets)

    audiosockets = find_sockets('audio')
    resolve_gids('audio', audiosockets, "sockets")
    audio_cdi = generate_sockets_cdi('audio', audiosockets)

    displaysockets = find_sockets('display')
    resolve_gids('display', displaysockets, "sockets")
    display_cdi = generate_sockets_cdi('display', displaysockets)

    # Check for DMA heap
    dmaheaps = find_devicenodes('/dev/dma_heap/*system')
    dmaheapgids = resolve_gids('dmaheap', dmaheaps)
    dmaheap_cdi = generate_devicenodes_cdi('dmaheap-system', dmaheaps,
                                           gidmap=dmaheapgids)

    # Check for DSP nodes
    cdsps = find_devicenodes('/dev/fastrpc-cdsp*')
    cdspgids = resolve_gids('fastrpc-cdsp', cdsps)
    cdsps_cdi = generate_devicenodes_cdi('fastrpc-cdsp', cdsps,
                                         gidmap=cdspgids)

    adsps = find_devicenodes('/dev/fastrpc-adsp*')
    adspgids = resolve_gids('fastrpc-adsp', adsps)
    adsps_cdi = generate_devicenodes_cdi('fastrpc-adsp', adsps,
                                         gidmap=adspgids)

    gdsps = find_devicenodes('/dev/fastrpc-gdsp*')
    gdspgids = resolve_gids('fastrpc-gdsp', gdsps)
    gdsps_cdi = generate_devicenodes_cdi('fastrpc-gdsp', gdsps,
                                         gidmap=gdspgids)

    # The board name, handed to the fastrpc containers twice over: as the
    # MACHINE_NAME their spec sets, and as the bind-mounted model node for
    # whatever reads the name itself. The read comes first, so a run logs the
    # board it believes it is on ahead of anything resolved from it.
    machinename = read_device_model()

    # The devicetree model node, bind-mounted so the board name is readable
    # inside the container (see generate_device_model_cdi()).
    devicemodel = generate_device_model_cdi()

    # The board configuration the hexagon-dsp packaging installs, and this
    # board's entry in it. The entry is read once: it names the firmware
    # directory below and the conf.d to hand over, both following from the file
    # that named this board (see read_dsp_library_path()).
    confdirs = find_dsp_conf_dirs()
    boardconf = read_dsp_library_path(machinename, confdirs)

    # That conf.d, handed to the container at one fixed path whichever of the
    # two locations the host keeps it in, so whatever resolves DSP_LIBRARY_PATH
    # in there reads the same file (see generate_dsp_conf_cdi()).
    dspconf = generate_dsp_conf_cdi(dsp_conf_mount_dir(boardconf, confdirs))

    # The firmware directory this board answers with: the one its configuration
    # named, or the directory the rpcd symlinks resolve to on an image carrying
    # no configuration (see dsp_firmware_board_dirs()).
    boarddirs = dsp_firmware_board_dirs(boardconf, machinename)

    # The mounts of each DSP class: the firmware files its own FastRPC client
    # opens, at the one container path they are presented under, plus the board
    # configuration and the board name. The host directory the files were found
    # in is not mounted, so a spec carries the firmware of that one DSP and no
    # path of another's.
    #
    # The files are resolved per class because the firmware is per DSP: which
    # of the board directory's files are a class's own is what
    # DSP_FIRMWARE_FILES says, and its keys are the class list.
    dspmounts = {}
    for dspclass in DSP_FIRMWARE_FILES:
        dspdirs = find_dsp_firmware_dirs(dspclass, boarddirs)
        dspmounts[dspclass] = (generate_dsp_files_cdi(dspclass, dspdirs)
                               + dspconf + devicemodel)

    # Write one CDI json per device class, each with the spec-level mounts that
    # class needs: the board name and the Hexagon binaries of its own DSP for
    # the fastrpc classes, and nothing for the rest. The camera, audio and
    # display sockets ride on their device entries instead, so requesting the
    # device is what mounts the socket.
    cdi_sections = [
        ('gpu',          gpu_cdi,     []),
        ('v4l2',         video_cdi,   []),
        ('camera',       camera_cdi,  []),
        ('audio',        audio_cdi,   []),
        ('display',      display_cdi, []),
        ('dmaheap',      dmaheap_cdi, []),
        ('fastrpc-cdsp', cdsps_cdi,   dspmounts['fastrpc-cdsp']),
        ('fastrpc-adsp', adsps_cdi,   dspmounts['fastrpc-adsp']),
        ('fastrpc-gdsp', gdsps_cdi,   dspmounts['fastrpc-gdsp']),
    ]
    cdi_sections = [(c, d, m) for c, d, m in cdi_sections
                    if c in enabled_classes]
    dynamiccdidir = Path(destdir).joinpath('run/cdi')
    cdifilename_stem = Path(cdifilename).stem
    cdifilename_suffix = Path(cdifilename).suffix

    # One file is written per device class (qualcomm-gpu.json, ...), so a
    # monolithic qualcomm.json in the output directory is stale. Remove it
    # rather than leave it to define conflicting devices alongside them: the
    # runtime reads every spec in the directory.
    legacy_cdi = dynamiccdidir.joinpath(cdifilename)
    if legacy_cdi.is_file():
        if args.dry_run:
            logging.info("Dry run: would remove old style monolithic CDI %s", legacy_cdi)
        else:
            logging.warning("Old style monolithic CDI detected, removing %s", legacy_cdi)
            legacy_cdi.unlink()

    # A host with no DSP node is not a target these specs are for, so nothing
    # is written at all -- not even the classes that have nothing to do with
    # the DSP. The stale monolithic file above is still removed, since leaving
    # it behind would be handing the runtime a spec too.
    if not cdsps and not adsps and not gdsps:
        logging.warning("No DSP nodes found (/dev/fastrpc-cdsp*, "
                        "/dev/fastrpc-adsp*, /dev/fastrpc-gdsp*); writing no "
                        "CDI specs")
        logging.info("Completed Qualcomm CDI generation")
        return 0

    for cdiclass, devices, classmounts in cdi_sections:
        if not devices:
            logging.debug("Skipping CDI file for '%s': no devices", cdiclass)
            continue
        cdi = build_cdi_spec(cdiclass, devices,
                             generate_enventries_cdi(cdiclass, machinename),
                             classmounts,
                             node_additional_gids(cdiclass,
                                                  sorted(classgids.get(cdiclass, ()))))
        section_filename = "%s-%s%s" % (cdifilename_stem, cdiclass, cdifilename_suffix)
        cdipath = dynamiccdidir.joinpath(section_filename)
        if args.dry_run:
            logging.info("Dry run: skipping CDI JSON write to %s", cdipath)
        else:
            Path(dynamiccdidir).mkdir(parents=True, exist_ok=True)
            with open(cdipath, "w") as cdifile:
                json.dump(cdi, cdifile, indent=JSON_INDENT)
                # json.dump() leaves no trailing newline, so the file would end
                # mid-line and read as truncated to the usual text tools.
                cdifile.write("\n")
            logging.info("Wrote CDI JSON: %s", cdipath)

    logging.info("Completed Qualcomm CDI generation")
    return 0

if __name__ == "__main__":
    sys.exit(main())
