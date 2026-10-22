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

import glob
import json
import os
from pathlib import Path
import re
import sys
import logging
import argparse
from typing import Callable

known_classes = ['gpu', 'v4l2', 'camera', 'audio', 'display', 'dmaheap',
                 'fastrpc-cdsp', 'fastrpc-adsp']

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

# Host sockets bind-mounted into the container, per device class. These classes
# are not reached through a device node at all: the container talks to a server
# on the host over a unix socket, so the socket is the whole resource and the
# spec carries a mount instead of a deviceNodes entry.
#
# The paths are the server-side defaults on a Qualcomm target:
#   * PulseAudio accepts clients on the per-user runtime socket,
#   * the camera server (qmmf/CamX) listens on /run/cam_server,
#   * the Wayland compositor exposes its socket in the user runtime dir.
# They are mounted at the same path inside the container so the usual client
# defaults (PULSE_SERVER, WAYLAND_DISPLAY, XDG_RUNTIME_DIR) resolve unchanged.
#
# A class whose socket is absent is skipped, exactly as a class whose device
# nodes are absent is: the server is not running, so the spec would hand the
# container a path that cannot be connected to.
CLASS_SOCKETS = {
    'audio':   ['/run/user/1000/pulse/native'],
    'camera':  ['/run/cam_server/le_camera_socket'],
    'display': ['/run/user/1000/wayland-0'],
}

# Mount options for the sockets above. Unlike the DSP firmware mounts these are
# read-write: a unix socket is useless to a client that cannot write to it.
SOCKET_MOUNT_OPTIONS = ["nosuid", "noexec", "rw", "bind"]

# V4L2 nodes that can belong to a camera. The kernel hands /dev/video0..31 to
# camera drivers and uses the higher numbers for the video encode/decode nodes,
# so the camera class only ever considers this range; see
# find_camera_devicenodes() for the driver check that decides which of them are
# really cameras.
CAMERA_NODE_RANGE = range(0, 32)

# Ownership and permissions applied to every generated deviceNodes entry. The
# container runtime uses these to create the node inside the container, so they
# describe the access the container should get rather than the host node's
# current state. The host device nodes are left untouched.
#
# Nodes are exposed as root-owned and group accessible. The group is taken from
# the host device node itself (see node_gid_map()), because that is the group
# the driver and its udev rules actually hand the device to, and it differs per
# board: a render node may be 'render' on one target and the container user's
# primary group on another. It can also differ *within* a class on one board,
# so the group is resolved per node rather than per class.
#
# CLASS_GID is only the fallback for when a node has no group of its own (it
# could not be stat()ed, or it is owned by group root), and keeps the values of
# the hand-maintained specs these defaults were taken from: the V4L2 and camera
# nodes belong to the 'video' group (44) while the render, DMA heap and Hexagon
# DSP nodes are handed to the container user's primary group (1000). The
# socket-only classes (audio, display) have no device node of their own; their
# fallback applies to the group owning the socket they mount.
DEFAULT_FILE_MODE = 0o660
DEFAULT_UID = 0
DEFAULT_GID = 1000
CLASS_GID = {
    'gpu':          1000,
    'v4l2':         44,
    'camera':       44,
    'audio':        1000,
    'display':      1000,
    'dmaheap':      1000,
    'fastrpc-cdsp': 1000,
    'fastrpc-adsp': 1000,
}

def node_gid_map(cdiclass: str, nodes: list[str] | None = None,
                 gid: int | None = None, what: str = "nodes") -> dict[str, int]:
    """Map each device node of one class to the group to grant for it.

    An explicit --gid always wins, for every node. Otherwise each node's group
    is read from the node itself, so the spec names the group that actually owns
    the device on this board rather than a compiled-in guess.

    The group is resolved per node because one class can span several groups on
    a single board: a target may expose /dev/video32 in 'video' while a camera
    node alongside it belongs to a vendor group. Collapsing those to one gid
    would emit the wrong owner for every node outside the winning group, so each
    node keeps its own and build_cdi_spec() grants the union (see
    node_additional_gids).

    The per-class CLASS_GID default stands in for any node with no group of its
    own: one that disappeared between the glob and the stat, or one owned by
    group root. A root-owned node is not honoured because the runtime drops a 0
    additionalGids entry, so a 0 here would grant an unprivileged container
    nothing.

    what names the paths in the log lines; the socket-backed classes pass
    'sockets' since they have no device node to speak of.
    """
    fallback = CLASS_GID.get(cdiclass, DEFAULT_GID)
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

def node_permissions(cdiclass: str, filemode: int | None = None,
                     uid: int | None = None, gid: int | None = None) -> dict:
    """Return the fileMode/uid/gid attributes for one device node.

    Explicit arguments (from the command line) win over the probed/built-in
    defaults, so a caller can force a single mode/owner across every class. gid
    is the group resolved by node_gid_map() for this node.
    """
    return {
        "fileMode": DEFAULT_FILE_MODE if filemode is None else filemode,
        "uid": DEFAULT_UID if uid is None else uid,
        "gid": CLASS_GID.get(cdiclass, DEFAULT_GID) if gid is None else gid,
    }

def node_additional_gids(cdiclass: str,
                         gids: int | list[int] | None = None) -> list[int]:
    """Return the additionalGids to grant for one device class.

    The nodes are emitted group accessible rather than world accessible, so the
    container process must be in the owning group to open them. Adding those
    gids to the container's supplementary groups keeps the nodes reachable
    without relaxing the mode on either side.

    Every group used by the class's deviceNodes entries is granted, not just
    one: additionalGids is a spec-level array while the gid is per node, so a
    class whose nodes span several groups needs all of them. Granting only the
    most common one would leave the rest unopenable for exactly the nodes that
    differ.

    gid 0 is dropped: the runtime skips a 0 entry when it builds the OCI spec,
    so emitting it would only add a no-op to every generated file.
    """
    if gids is None:
        gids = [CLASS_GID.get(cdiclass, DEFAULT_GID)]
    elif isinstance(gids, int):
        gids = [gids]
    return sorted({nodegid for nodegid in gids if nodegid})

def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(format="%(levelname)s: %(message)s", level=level)

def parse_octal_mode(value: str) -> int:
    """Parse a file mode from the command line, defaulting to octal.

    Modes are conventionally written in octal ('660', '0660'), so bare digits
    are read base 8; an explicit prefix ('0o660', '0x1b0') is honoured too.
    Anything else is rejected rather than quietly reinterpreted -- '668' looks
    like a mode but is not octal, and accepting it as decimal would hand back a
    silently different permission set.
    """
    if re.fullmatch(r'0?[0-7]+', value):
        return int(value, 8)
    if re.fullmatch(r'0[oxb][0-9a-fA-F]+', value):
        try:
            return int(value, 0)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        "invalid file mode: %r (expected octal, e.g. 660)" % value)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Qualcomm CDI specification files")
    parser.add_argument("-d", "--destdir", default="/", help="Destination root directory (default: %(default)s)")
    parser.add_argument("-c", "--cdifilename", default="qualcomm.json", help="CDI JSON filename base; the device class is inserted before the extension, e.g. qualcomm.json -> qualcomm-gpu.json (default: %(default)s)")
    parser.add_argument("-C", "--classes", default=None, help="Comma-separated list of CDI classes to generate (default: all). Available: %s" % ", ".join(known_classes))
    parser.add_argument("-m", "--filemode", type=parse_octal_mode, default=None, metavar="MODE", help="fileMode for generated device nodes, parsed as octal (default: %#o)" % DEFAULT_FILE_MODE)
    parser.add_argument("-u", "--uid", type=int, default=None, help="uid for generated device nodes (default: %d)" % DEFAULT_UID)
    parser.add_argument("-g", "--gid", type=int, default=None, metavar="GID", help="gid for generated device nodes and additionalGids, overriding the group read from the host device nodes")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Parse and probe devices but do not write any files")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv)")
    return parser.parse_args()

def find_devicenodes(deviceglob: str) -> list[str]:
    logging.debug("Globbing device nodes: %s", deviceglob)
    files = glob.glob(deviceglob)
    logging.info("Found %d nodes for pattern %s", len(files), deviceglob)
    logging.debug("Nodes for %s: %s", deviceglob, files)
    return files

def devicenode_driver_module(node: str) -> str | None:
    """Return the resolved sysfs driver module path backing a V4L2 node.

    Mirrors the readlink in check-camx-overlay.sh as shipped on target:

        readlink -f /sys/class/video4linux/$(basename $dev)/device/driver/module

    None is returned when any link in that chain is missing, which is the
    script's 2>/dev/null case: a node whose driver exposes no module (built-in
    rather than a module) simply is not a camera by this test.
    """
    modulelink = Path('/sys/class/video4linux', Path(node).name,
                      'device', 'driver', 'module')
    try:
        # resolve(strict=True) follows the whole chain like 'readlink -f' and
        # raises if any component is missing, which is the script's
        # 2>/dev/null case.
        resolved = modulelink.resolve(strict=True)
    except OSError as err:
        logging.debug("No driver module for %s: %s", node, err)
        return None
    return str(resolved)

def is_camera_devicenode(node: str) -> bool:
    """Report whether one V4L2 node is backed by a camera driver.

    This is the check from /usr/libexec/qmmf-server/check-camx-overlay.sh: the
    node's driver module path is resolved through sysfs and matched against the
    substring 'camera'. On a CamX target the camera nodes resolve to a module
    whose name carries that keyword, while the video encode/decode nodes on the
    same board do not, so this distinguishes a real camera from the rest of
    /dev/video*.
    """
    module = devicenode_driver_module(node)
    if module is None:
        return False
    iscamera = 'camera' in module
    logging.debug("Driver module for %s: %s (camera: %s)", node, module, iscamera)
    return iscamera

def find_camera_devicenodes(videonodes: list[str]) -> list[str]:
    """Select the camera nodes out of the probed V4L2 nodes.

    Two filters apply, in this order:

      * only /dev/video0..31 are considered (CAMERA_NODE_RANGE). The kernel
        hands the low numbers to camera drivers and the high ones to the video
        encode/decode devices.
      * of those, only nodes whose driver module names a camera are kept, using
        the same sysfs probe as check-camx-overlay.sh.

    An empty list means no camera was detected, and the caller then writes no
    camera spec at all rather than one pointing at nodes that are not cameras.
    """
    inrange = [node for node in videonodes
               if get_devicenode_index(node) in CAMERA_NODE_RANGE]
    logging.debug("V4L2 nodes in camera range %d-%d: %s",
                  CAMERA_NODE_RANGE.start, CAMERA_NODE_RANGE.stop - 1, inrange)
    cameras = [node for node in inrange if is_camera_devicenode(node)]
    logging.info("Found %d camera node(s) of %d V4L2 node(s) in range",
                 len(cameras), len(inrange))
    return cameras

def find_sockets(cdiclass: str) -> list[str]:
    """Return the host sockets to mount for one class, keeping only those present.

    A missing socket means the host-side server is not running, so there is
    nothing for the container to connect to and the class is skipped. This is
    the socket equivalent of an empty device node glob.
    """
    sockets = []
    for path in CLASS_SOCKETS.get(cdiclass, []):
        if Path(path).exists():
            logging.info("Found %s socket: %s", cdiclass, path)
            sockets.append(path)
        else:
            logging.debug("No %s socket at %s", cdiclass, path)
    return sockets

def generate_mounts_cdi(sockets: list[str]) -> list[dict]:
    """Build the containerEdits mount entries for a list of host sockets.

    Each socket is bind-mounted at the same path inside the container, so a
    client using the conventional default (PULSE_SERVER, WAYLAND_DISPLAY) finds
    it without extra configuration. The mounts are read-write because a unix
    socket cannot be used by a client that may not write to it.
    """
    return [{"hostPath": socket, "containerPath": socket,
             "options": list(SOCKET_MOUNT_OPTIONS)} for socket in sockets]

def generate_sockets_cdi(nickname: str, sockets: list[str]) -> list[dict]:
    """Build the CDI device entries for a socket-only class.

    The audio and display classes pass through no device node at all, so their
    device entry carries the socket mount rather than a deviceNodes list. The
    container asks for qualcomm.com/audio=audio and gets the socket; without a
    device entry the class could not be requested on the command line at all,
    since a runtime only exposes named devices.

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
                             permissions: dict | Callable[[str], dict] | None = None,
                             pernode: bool = True,
                             catchall: bool = True) -> list[dict]:
    """Build the CDI device entries for a list of device nodes.

    nickname:    base name for the emitted devices, suffixed with the node index
    filesglob:   the device nodes to expose
    permissions: fileMode/uid/gid for the nodes; a dict shared by all of them, a
                 callable asked per node, or None for the bare path-only form
    pernode:     emit one device per node (renderD128, video0, ...)
    catchall:    emit the '<nickname>:all' device covering every node at once

    A class assembled from several globs calls this once per glob, with
    catchall=False on all but one so the ':all' device is not defined twice, and
    a final pernode=False call to emit a single catch-all spanning the lot.
    """
    if filesglob:
        logging.info("Generating CDI entries for '%s' with %d node(s)", nickname, len(filesglob) if filesglob else 0)
        filesglob_set = set(filesglob)

        # Ownership/permission attributes attached to every emitted node. A
        # plain dict applies to every node; a callable is asked per node, which
        # is how main() gives each node the group that owns it on this board.
        # None keeps the bare {"path": ...} form used before these were added,
        # which the CDI schema still accepts.
        if permissions is None:
            nodeattrs = lambda node: {}
        elif callable(permissions):
            nodeattrs = permissions
        else:
            nodeattrs = lambda node: permissions

        # -secure nodes whose non-secure parent is also present are handled as siblings of
        # that parent entry, not as independent top-level entries
        def is_sibling(node: str) -> bool:
            return node.endswith('-secure') and node[:-len('-secure')] in filesglob_set

        def node_entry(node: str) -> dict:
            # One deviceNodes entry: the path plus this node's fileMode/uid/gid.
            # The dict is rebuilt per entry so callers cannot alias a single
            # attribute dict across every generated node.
            return dict({"path": node}, **nodeattrs(node))

        def node_paths(node: str) -> list[dict]:
            # deviceNodes entries for one node: the node itself, plus its -secure
            # sibling when this is a cdsp/adsp node and that sibling is present.
            paths = [node_entry(node)]
            if node.endswith(('cdsp', 'adsp')):
                securepath = node + "-secure"
                if securepath in filesglob_set:
                    logging.debug("DSP node detected, adding -secure variant for %s", node)
                    paths.append(node_entry(securepath))
            return paths

        # Count only nodes that will produce their own CDI entry
        effective_count = sum(1 for n in filesglob if not is_sibling(n))

        devicenodelist = []
        devicenodeindex = 0
        for devicenode in sorted(filesglob) if pernode else []:
            if is_sibling(devicenode):
                continue
            device_pathlist = { "deviceNodes": node_paths(devicenode) }
            cdi_index = get_devicenode_index(devicenode)
            # If there's only one match *and* it doesn't have its own index, don't add the '0' index
            if effective_count == 1 and cdi_index is None:
                # Empty string means str(cdi_index) appends nothing, giving just 'nickname'
                cdi_index = ""
            # Reuse the devicenode index if present, otherwise generate our own
            if cdi_index is not None:
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

def build_cdi_spec(cdiclass: str, devices: list[dict],
                   enventries: list[str], mountentries: list[dict | None],
                   additionalgids: list[int] | None = None) -> dict:
    """Assemble a single CDI specification dict for one device class.

    The returned structure follows the CDI spec format defined by the CNCF
    Container Device Interface project:
        https://github.com/cncf-tags/container-device-interface

    cdiclass:       device class name, suffixed onto CDI_VENDOR for the 'kind'
    devices:        list of CDI device entries (as built by generate_devicenodes_cdi)
    enventries:     list of "KEY=value" strings added to containerEdits.env
    mountentries:   list of mount dicts attached to containerEdits.mounts; the
                    caller decides what belongs to this class (DSP firmware for
                    the fastrpc classes, a host socket for audio/camera/display)
    additionalgids: supplementary groups the container needs to open the nodes

    Access is expressed entirely through the spec: each deviceNodes entry
    carries fileMode/uid/gid and the container joins every group owning one of
    them via additionalGids. Nothing is changed on the host filesystem.

    This function is pure (no filesystem access) so the generated spec can be
    validated by the upstream 'cdi' tool in the unit tests.
    """
    cdi = {"cdiVersion": CDI_VERSION, "kind": CDI_VENDOR + "/" + cdiclass}
    cdi["devices"] = devices
    container_edits = {"env": enventries}
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
    logging.info("Node attributes: fileMode=%s, uid=%s, gid=%s",
                 "%#o" % args.filemode if args.filemode is not None else "%#o (default)" % DEFAULT_FILE_MODE,
                 args.uid if args.uid is not None else "%d (default)" % DEFAULT_UID,
                 args.gid if args.gid is not None else "from host device nodes")

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

    # Ownership/permissions applied to the generated device nodes. The group is
    # resolved per path from the host paths just probed, so every node and
    # socket keeps the group it is actually handed to on this board unless it is
    # overridden on the CLI. classgids collects the groups each class ended up
    # using, which become that spec's additionalGids.
    classgids = {}

    def resolve_gids(cdiclass: str, paths: list[str],
                     what: str = "nodes") -> dict[str, int]:
        """Resolve the group of every path of a class and record the union."""
        gidmap = node_gid_map(cdiclass, paths, args.gid, what)
        classgids.setdefault(cdiclass, set()).update(gidmap.values())
        return gidmap

    def perms_for(cdiclass: str, gidmap: dict[str, int]) -> Callable[[str], dict]:
        """Per-node attribute callable backed by an already-resolved gid map."""
        fallback = CLASS_GID.get(cdiclass, DEFAULT_GID) if args.gid is None else args.gid

        def nodeperms(node: str) -> dict:
            return node_permissions(cdiclass, args.filemode, args.uid,
                                    gidmap.get(node, fallback))
        return nodeperms

    def perms(cdiclass: str, nodes: list[str]) -> Callable[[str], dict]:
        """Resolve groups for a class's nodes and return its attribute callable."""
        return perms_for(cdiclass, resolve_gids(cdiclass, nodes))

    # Find the GPU nodes. The render node is the one a container renders
    # through, but a full GPU stack also needs the DRM card node, and on a
    # Qualcomm target the Adreno KGSL node when the kernel exposes one (older
    # downstream kernels use /dev/kgsl-3d0 rather than going through DRM).
    rendernodes = find_devicenodes('/dev/dri/renderD*')
    cardnodes = find_devicenodes('/dev/dri/card*')
    kgslnodes = find_devicenodes('/dev/kgsl-3d*')
    gpunodes = rendernodes + cardnodes + kgslnodes
    # One gid map for the whole class: the nodes often differ (a render node in
    # 'render' next to a card node in 'video'), and additionalGids has to carry
    # every one of those groups.
    gpuperms = perms('gpu', gpunodes)
    # Keep the established renderD* device names, and their renderD:all
    # catch-all, so specs that already reference them keep working. The card
    # and KGSL nodes are added alongside under their own names, plus a single
    # 'gpu:all' that hands over the whole GPU stack at once.
    render_cdi = generate_devicenodes_cdi('renderD', rendernodes, gpuperms)
    card_cdi = generate_devicenodes_cdi('card', cardnodes, gpuperms, catchall=False)
    kgsl_cdi = generate_devicenodes_cdi('kgsl-3d', kgslnodes, gpuperms, catchall=False)
    gpu_all_cdi = generate_devicenodes_cdi('gpu', gpunodes, gpuperms, pernode=False)
    gpu_cdi = render_cdi + card_cdi + kgsl_cdi + gpu_all_cdi

    # Find all videonoodes and generate entries
    # The v4l2 class carries every node, encoders and decoders included; the
    # camera class below narrows this down to the nodes a camera driver owns.
    videonodes = find_devicenodes('/dev/video*')
    video_cdi = generate_devicenodes_cdi('video', videonodes, perms('v4l2', videonodes))

    # Narrow the V4L2 nodes down to the cameras, using the same sysfs driver
    # probe as check-camx-overlay.sh on target. The camera server socket is
    # mounted alongside them: a client talks to qmmf/CamX over that socket, so
    # the nodes alone are not enough to use the camera.
    cameranodes = find_camera_devicenodes(videonodes)
    camerasockets = find_sockets('camera')
    cameraperms = perms_for('camera', resolve_gids(
        'camera', cameranodes + camerasockets, "nodes and sockets"))
    camera_cdi = generate_devicenodes_cdi('camera', cameranodes, cameraperms)
    camera_mounts = generate_mounts_cdi(camerasockets)

    # Socket-only classes. There is no device node to pass through: the
    # container reaches the host audio server and compositor over a unix
    # socket, so the bind-mounted socket is the whole device and it rides on
    # the device entry itself.
    audiosockets = find_sockets('audio')
    resolve_gids('audio', audiosockets, "sockets")
    audio_cdi = generate_sockets_cdi('audio', audiosockets)

    displaysockets = find_sockets('display')
    resolve_gids('display', displaysockets, "sockets")
    display_cdi = generate_sockets_cdi('display', displaysockets)

    # Check for DMA heap
    dmaheaps = find_devicenodes('/dev/dma_heap/*system')
    dmaheap_cdi = generate_devicenodes_cdi('dmaheap-system', dmaheaps, perms('dmaheap', dmaheaps))

    # Check for DSP nodes
    cdsps = find_devicenodes('/dev/fastrpc-cdsp*')
    cdsps_cdi = generate_devicenodes_cdi('fastrpc-cdsp', cdsps, perms('fastrpc-cdsp', cdsps))

    adsps = find_devicenodes('/dev/fastrpc-adsp*')
    adsps_cdi = generate_devicenodes_cdi('fastrpc-adsp', adsps, perms('fastrpc-adsp', adsps))

    # Bind mounts into container
    # The primary use case is passing tightly coupled files into the
    # container, notably Hexagon binaries. The Hexagon binaries are
    # tightly coupled to the files loaded by the in-kernel firmware
    # loader. To lower the chance of a mismatch, Hexagon binaries found
    # on the host will be bind mounted automatically
    # Bind mount devicetree model if present
    devicetreefound = 0
    dtmodelstring = None
    # glob.glob() always returns a list; check for non-empty to confirm the file exists
    dtmodel = glob.glob("/sys/firmware/devicetree/base/model")
    if dtmodel:
        devicetreefound = 1
        dtmodelmount ={"hostPath": "/sys/firmware/devicetree/base/model" , "containerPath": "/run/device-model", "options": ["nosuid", "ro", "bind"]}
        modeldtnode = open("/sys/firmware/devicetree/base/model", "r")
        # remove literal Null terminator during read
        dtmodelstring = str(modeldtnode.read()).replace('\u0000', '')
        logging.info("Detected %s from devicetree", dtmodelstring)
        modeldtnode.close()

    # Glob for Hexagon DSP firmware directories; the four wildcard levels match
    # vendor/package/version/arch sub-paths under /usr/share
    localfiles = find_devicenodes('/usr/share/*/*/*/*/dsp/')
    mountentries = [None] * (len(localfiles) + devicetreefound)
    mountentry = 0
    for localfile in localfiles:
        mountentries[mountentry] ={"hostPath": localfile , "containerPath": localfile, "options": ["nosuid", "ro", "bind"]}
        mountentry += 1
    if devicetreefound > 0:
        mountentries[mountentry] = dtmodelmount


    enventries = []
    if dtmodelstring is not None:
        enventries = [ "MACHINE_NAME=" + dtmodelstring ]

    # Write one CDI json per device class, each with the mounts that class
    # needs: the Hexagon binaries and devicetree model for the fastrpc classes,
    # the camera server socket for the camera class, and nothing for the rest.
    # The audio and display sockets ride on their device entries instead, so
    # requesting the device is what mounts the socket.
    cdi_sections = [
        ('gpu',          gpu_cdi,     []),
        ('v4l2',         video_cdi,   []),
        ('camera',       camera_cdi,  camera_mounts),
        ('audio',        audio_cdi,   []),
        ('display',      display_cdi, []),
        ('dmaheap',      dmaheap_cdi, []),
        ('fastrpc-cdsp', cdsps_cdi,   mountentries),
        ('fastrpc-adsp', adsps_cdi,   mountentries),
    ]
    cdi_sections = [(c, d, m) for c, d, m in cdi_sections if c in enabled_classes]
    dynamiccdidir = Path(destdir).joinpath('run/cdi')
    cdifilename_stem = Path(cdifilename).stem
    cdifilename_suffix = Path(cdifilename).suffix

    # Older versions wrote a single monolithic CDI file (e.g. qualcomm.json); we
    # now write one file per device class (qualcomm-gpu.json, ...). Remove any
    # leftover monolithic file so it cannot define conflicting/stale devices
    # alongside the per-class files.
    legacy_cdi = dynamiccdidir.joinpath(cdifilename)
    if legacy_cdi.is_file():
        if args.dry_run:
            logging.info("Dry run: would remove old style monolithic CDI %s", legacy_cdi)
        else:
            logging.warning("Old style monolithic CDI detected, removing %s", legacy_cdi)
            legacy_cdi.unlink()

    for cdiclass, devices, classmounts in cdi_sections:
        if not devices:
            logging.debug("Skipping CDI file for '%s': no devices", cdiclass)
            continue
        cdi = build_cdi_spec(cdiclass, devices, enventries, classmounts,
                             node_additional_gids(cdiclass, sorted(classgids.get(cdiclass, ()))))
        section_filename = "%s-%s%s" % (cdifilename_stem, cdiclass, cdifilename_suffix)
        cdipath = dynamiccdidir.joinpath(section_filename)
        if args.dry_run:
            logging.info("Dry run: skipping CDI JSON write to %s", cdipath)
        else:
            Path(dynamiccdidir).mkdir(parents=True, exist_ok=True)
            with open(cdipath, "w") as cdifile:
                cdifile.write(json.dumps(cdi))
            logging.info("Wrote CDI JSON: %s", cdipath)

    logging.info("Completed Qualcomm CDI generation")
    return 0

if __name__ == "__main__":
    sys.exit(main())
