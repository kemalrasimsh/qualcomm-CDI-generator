# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
#
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""Stand-alone unit tests for the Qualcomm CDI generator.

The tests are split in two groups:

  * Structural tests that exercise the spec-building helpers directly and need
    no external tooling.
  * Validation tests that write generated CDI JSON files and run the upstream
    'cdi' tool from the CNCF Container Device Interface project to confirm the
    output conforms to the CDI specification:
        https://github.com/cncf-tags/container-device-interface

The 'cdi' tool is located via the CDI_TOOL environment variable (an absolute
path to the binary) or, failing that, the first 'cdi' found on PATH. When the
tool cannot be found the validation tests are skipped rather than failed, so
the suite remains usable in environments where 'cdi' is not installed.

Run from the repository root with:

    python3 -m unittest discover -s tests -v

or point the suite at a freshly built tool:

    CDI_TOOL=/path/to/cdi python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_generator():
    """Import the generator script as a module despite its hyphenated name."""
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "qualcomm-cdi-generator.py"
    if not script.is_file():
        raise FileNotFoundError("generator script not found at %s" % script)
    spec = importlib.util.spec_from_file_location("qualcomm_cdi_generator", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_cdi_tool():
    """Return the path to the 'cdi' validation tool, or None if unavailable."""
    env_tool = os.environ.get("CDI_TOOL")
    if env_tool:
        # An explicitly configured tool that does not exist is a hard error:
        # the caller asked for a specific binary and we should not silently skip.
        if not (os.path.isfile(env_tool) and os.access(env_tool, os.X_OK)):
            raise FileNotFoundError("CDI_TOOL=%s is not an executable file" % env_tool)
        return env_tool
    return shutil.which("cdi")


gen = _load_generator()
CDI_TOOL = _find_cdi_tool()


# Device probes returned by a fully-populated synthetic host; used to drive
# main() deterministically regardless of the hardware the tests run on.
FAKE_NODES = {
    "/dev/dri/renderD*": ["/dev/dri/renderD128"],
    "/dev/dri/card*": ["/dev/dri/card0"],
    "/dev/kgsl-3d*": ["/dev/kgsl-3d0"],
    "/dev/video*": ["/dev/video0", "/dev/video1"],
    "/dev/dma_heap/*system": ["/dev/dma_heap/system"],
    "/dev/fastrpc-cdsp*": ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"],
    "/dev/fastrpc-adsp*": ["/dev/fastrpc-adsp"],
    "/usr/share/*/*/*/*/dsp/": [],
}

# Host sockets the socket-backed classes look for. Keyed by class so a test can
# present or withhold each one; withheld means the server is not running.
FAKE_SOCKETS = {
    "audio": "/run/user/1000/pulse/native",
    "camera": "/run/cam_server/le_camera_socket",
    "display": "/run/user/1000/wayland-0",
}


def fake_stat(gids, fake_nodes=FAKE_NODES, sockets=()):
    """Return an os.stat replacement reporting controlled device node groups.

    The generator reads the owning group of the nodes it probes, and some of
    those paths (notably /dev/dri/renderD128) really do exist on a developer
    machine with whatever group that host happens to use. Stat the synthetic
    node list from a fixed mapping instead, so the resolved group is the one the
    test asked for rather than the test machine's.

    gids maps a node path to the gid to report; any other synthetic node or
    socket raises OSError as if it were absent, which is what the generator sees
    on a host where the class has no nodes. Every unrelated path (the temporary
    output directory, anything pathlib stats internally) goes to the real
    os.stat.
    """
    real_stat = os.stat
    synthetic = {node for nodes in fake_nodes.values() for node in nodes}
    synthetic.update(sockets)

    def stat(path, *args, **kwargs):
        key = os.fspath(path) if isinstance(path, (str, os.PathLike)) else path
        if key in gids:
            return mock.Mock(st_gid=gids[key])
        if key in synthetic:
            raise FileNotFoundError(2, "No such file or directory", key)
        return real_stat(path, *args, **kwargs)

    return stat


def fake_stat_all(gid):
    """Map every synthetic node to one gid, for run_generator(node_gids=...)."""
    return {node: gid for nodes in FAKE_NODES.values() for node in nodes}


# Every probed node owned by group 110. 110 matches no per-class default, so a
# group that was really resolved from a node can never be confused with the
# fallback the generator would have used anyway.
ALL_NODES_GID = 110
ALL_NODES_STAT = fake_stat_all(ALL_NODES_GID)


def run_generator(extra_argv, fake_nodes=FAKE_NODES, node_gids=None,
                  sockets=(), cameras=()):
    """Run main() with synthetic device discovery into a fresh argv.

    find_devicenodes is replaced with controlled node lists and the direct
    glob.glob() devicetree probe is stubbed empty, so the run does not depend
    on the host hardware. node_gids maps node paths to the group to report for
    them; nodes left out of it stat as absent, so the generator falls back to
    its per-class default group.

    sockets lists the host sockets that exist for this run (paths, as in
    FAKE_SOCKETS.values()); any socket not listed is reported absent, which is
    what the generator sees when the host-side server is not running. cameras
    lists the /dev/video* nodes whose driver module names a camera, standing in
    for the sysfs probe that check-camx-overlay.sh performs on target.

    Returns main()'s exit code.
    """
    real_find = gen.find_devicenodes

    def fake_find(deviceglob):
        # Controlled lists for known probes; fall back to the real glob for
        # anything unexpected so a typo surfaces as a test error.
        return list(fake_nodes[deviceglob]) if deviceglob in fake_nodes \
            else real_find(deviceglob)

    real_glob = gen.glob.glob

    def fake_glob(pattern, *args, **kwargs):
        if pattern == "/sys/firmware/devicetree/base/model":
            return []
        return real_glob(pattern, *args, **kwargs)

    socketset = set(sockets)

    def fake_find_sockets(cdiclass):
        return [path for path in gen.CLASS_SOCKETS.get(cdiclass, [])
                if path in socketset]

    cameraset = set(cameras)

    with mock.patch.object(gen, "find_devicenodes", fake_find), \
            mock.patch.object(gen.glob, "glob", fake_glob), \
            mock.patch.object(gen, "find_sockets", fake_find_sockets), \
            mock.patch.object(gen, "is_camera_devicenode", cameraset.__contains__), \
            mock.patch.object(gen.os, "stat",
                              fake_stat(node_gids or {}, fake_nodes, socketset)), \
            mock.patch.object(sys, "argv", ["qualcomm-cdi-generator.py"] + extra_argv):
        # main() calls setup_logging(), which would print the generator's log
        # lines to the test console; keep the suite output clean by muting it.
        logging.disable(logging.CRITICAL)
        try:
            return gen.main()
        finally:
            logging.disable(logging.NOTSET)


class StructuralTests(unittest.TestCase):
    """Tests for the pure spec-building helpers; no external tooling needed."""

    def test_kind_and_version(self):
        spec = gen.build_cdi_spec("gpu", [], [], [])
        self.assertEqual(spec["cdiVersion"], gen.CDI_VERSION)
        self.assertEqual(spec["kind"], gen.CDI_VENDOR + "/gpu")

    def test_no_hooks_are_emitted(self):
        # Access is granted through fileMode/uid/gid and additionalGids alone;
        # the spec must never ask the runtime to run a host-side hook script.
        spec = gen.build_cdi_spec("gpu", [], [], [])
        self.assertNotIn("hooks", spec["containerEdits"])

    def test_additional_gids_emitted(self):
        spec = gen.build_cdi_spec("v4l2", [], [], [], [44])
        self.assertEqual(spec["containerEdits"]["additionalGids"], [44])

    def test_additional_gids_omitted_when_empty(self):
        # An empty list must not leave a bare "additionalGids": [] behind.
        for gids in (None, []):
            spec = gen.build_cdi_spec("gpu", [], [], [], gids)
            self.assertNotIn("additionalGids", spec["containerEdits"])

    def test_class_additional_gids(self):
        # The supplementary group follows the node's owning group per class.
        self.assertEqual(gen.node_additional_gids("v4l2"), [44])
        self.assertEqual(gen.node_additional_gids("gpu"), [1000])
        # An explicit --gid override wins, as it does for the node attributes.
        self.assertEqual(gen.node_additional_gids("v4l2", 1001), [1001])
        # Unknown classes fall back to the default gid.
        self.assertEqual(gen.node_additional_gids("brandnew"), [gen.DEFAULT_GID])

    def test_node_gid_read_from_host_node(self):
        # The group comes from the device node on this board, not the built-in
        # per-class default: the same class is handed to a different group
        # depending on the target's udev rules.
        with mock.patch.object(gen.os, "stat",
                               fake_stat({"/dev/dri/renderD128": 110})):
            self.assertEqual(gen.node_gid_map("gpu", ["/dev/dri/renderD128"]),
                             {"/dev/dri/renderD128": 110})

    def test_node_gid_override_wins_over_host_node(self):
        stat = mock.Mock(side_effect=fake_stat({"/dev/dri/renderD128": 110}))
        with mock.patch.object(gen.os, "stat", stat):
            self.assertEqual(
                gen.node_gid_map("gpu", ["/dev/dri/renderD128"], 1001),
                {"/dev/dri/renderD128": 1001})
            # An explicit --gid short-circuits the probe entirely.
            stat.assert_not_called()

    def test_node_gid_falls_back_to_class_default(self):
        # A node that cannot be stat()ed (it vanished between the glob and the
        # stat) falls back to the class default.
        with mock.patch.object(gen.os, "stat", fake_stat({})):
            self.assertEqual(gen.node_gid_map("v4l2", ["/dev/video0"]),
                             {"/dev/video0": 44})
        # No nodes at all means nothing to map.
        self.assertEqual(gen.node_gid_map("v4l2", []), {})
        self.assertEqual(gen.node_gid_map("v4l2", None), {})
        # Unknown classes have no entry in CLASS_GID.
        with mock.patch.object(gen.os, "stat", fake_stat({})):
            self.assertEqual(gen.node_gid_map("brandnew", ["/dev/video0"]),
                             {"/dev/video0": gen.DEFAULT_GID})

    def test_node_gid_ignores_root_owned_node(self):
        # A root:root node has no group to join, and the runtime drops a 0
        # additionalGids entry, so honouring it would grant an unprivileged
        # container nothing. Use the class default for that node instead.
        with mock.patch.object(gen.os, "stat",
                               fake_stat({"/dev/dma_heap/system": 0})):
            self.assertEqual(gen.node_gid_map("dmaheap", ["/dev/dma_heap/system"]),
                             {"/dev/dma_heap/system": 1000})

    def test_node_gid_keeps_every_group_of_a_mixed_class(self):
        # One class can span several groups on a single board: a target may put
        # /dev/video32 in 'video' and a camera node alongside it in a vendor
        # group. Each node keeps the group that actually owns it rather than
        # being collapsed onto one winner.
        nodes = ["/dev/video32", "/dev/video33", "/dev/video34"]
        gids = {"/dev/video32": 44, "/dev/video33": 44, "/dev/video34": 1002}
        with mock.patch.object(gen.os, "stat", fake_stat(gids)):
            self.assertEqual(gen.node_gid_map("v4l2", nodes), gids)

    def test_additional_gids_grants_every_group_of_a_mixed_class(self):
        # additionalGids is a spec-level array while the gid is per node, so a
        # class spanning several groups must grant all of them. Granting only
        # the most common one leaves the differing nodes unopenable.
        self.assertEqual(gen.node_additional_gids("v4l2", [44, 1002, 44]),
                         [44, 1002])
        # Order is stable and a root gid is still dropped from the set.
        self.assertEqual(gen.node_additional_gids("v4l2", [1002, 0, 44]),
                         [44, 1002])

    def test_additional_gids_drops_root(self):
        # The runtime skips a 0 gid, so emitting it would be a no-op entry.
        self.assertEqual(gen.node_additional_gids("gpu", 0), [])

    def test_additional_gids_match_node_gid(self):
        # The group the container joins must be the group that owns the node,
        # otherwise the 0660 node stays unreadable inside the container.
        for cdiclass in gen.known_classes:
            self.assertEqual(gen.node_additional_gids(cdiclass),
                             [gen.node_permissions(cdiclass)["gid"]], cdiclass)

    def test_spec_version_supports_additional_gids(self):
        # additionalGids was introduced in CDI 0.7.0; emitting it under an
        # older version makes the spec fail validation.
        self.assertGreaterEqual(
            tuple(int(p) for p in gen.CDI_VERSION.split(".")), (0, 7, 0))

    def test_mounts_attached_when_given(self):
        # build_cdi_spec attaches whatever mounts the caller passes; main()
        # decides what belongs to each class (DSP firmware for fastrpc, the
        # camera socket for camera, nothing for the rest).
        mounts = [{"hostPath": "/x", "containerPath": "/x", "options": ["ro", "bind"]}]
        cdsp = gen.build_cdi_spec("fastrpc-cdsp", [], [], mounts)
        self.assertEqual(cdsp["containerEdits"]["mounts"], mounts)

    def test_mounts_omitted_when_empty(self):
        # A class with no mounts must not leave a bare "mounts": [] behind.
        for mounts in ([], None, [None]):
            spec = gen.build_cdi_spec("gpu", [], [], mounts)
            self.assertNotIn("mounts", spec["containerEdits"], repr(mounts))

    def test_mount_none_placeholders_filtered(self):
        # main() pre-allocates the mount list with None placeholders; build_cdi_spec
        # must strip them so the emitted spec never contains null mount entries.
        mounts = [None, {"hostPath": "/x", "containerPath": "/x", "options": ["ro", "bind"]}, None]
        cdsp = gen.build_cdi_spec("fastrpc-cdsp", [], [], mounts)
        self.assertEqual(len(cdsp["containerEdits"]["mounts"]), 1)
        self.assertNotIn(None, cdsp["containerEdits"]["mounts"])

    def test_env_passthrough(self):
        env = ["MACHINE_NAME=Synthetic Board"]
        spec = gen.build_cdi_spec("fastrpc-adsp", [], env, [])
        self.assertEqual(spec["containerEdits"]["env"], env)

    def test_single_unnamed_node_has_no_index(self):
        # A lone node without a trailing number should not gain a '0' suffix.
        devices = gen.generate_devicenodes_cdi("dmaheap-system", ["/dev/dma_heap/system"])
        names = [d["name"] for d in devices]
        self.assertIn("dmaheap-system", names)
        self.assertIn("dmaheap-system:all", names)

    def test_secure_sibling_folded_into_parent(self):
        # /dev/fastrpc-cdsp-secure must ride along with its non-secure parent
        # rather than producing an independent top-level entry.
        nodes = ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"]
        devices = gen.generate_devicenodes_cdi("fastrpc-cdsp", nodes)
        named = {d["name"]: d for d in devices}
        # No entry should be named after the -secure node directly.
        self.assertNotIn("fastrpc-cdsp-secure", named)
        parent = named["fastrpc-cdsp"]
        paths = [n["path"] for n in parent["containerEdits"]["deviceNodes"]]
        self.assertIn("/dev/fastrpc-cdsp", paths)
        self.assertIn("/dev/fastrpc-cdsp-secure", paths)

    def test_class_default_permissions(self):
        # The v4l2 nodes belong to the 'video' group while the render nodes go
        # to the container user's group; both are root-owned and 0660.
        self.assertEqual(gen.node_permissions("v4l2"),
                         {"fileMode": 0o660, "uid": 0, "gid": 44})
        self.assertEqual(gen.node_permissions("gpu"),
                         {"fileMode": 0o660, "uid": 0, "gid": 1000})

    def test_permission_overrides(self):
        # Explicit CLI values win over the per-class defaults.
        self.assertEqual(gen.node_permissions("v4l2", 0o666, 1000, 1001),
                         {"fileMode": 0o666, "uid": 1000, "gid": 1001})
        # A partial override leaves the untouched fields at their defaults.
        self.assertEqual(gen.node_permissions("v4l2", None, None, 100),
                         {"fileMode": 0o660, "uid": 0, "gid": 100})

    def test_unknown_class_falls_back_to_default_gid(self):
        self.assertEqual(gen.node_permissions("brandnew")["gid"], gen.DEFAULT_GID)

    def test_permissions_applied_to_every_node(self):
        # Both the per-node entries and the ':all' catch-all carry the attributes.
        perms = gen.node_permissions("v4l2")
        devices = gen.generate_devicenodes_cdi("video", ["/dev/video0", "/dev/video1"], perms)
        emitted = [n for d in devices for n in d["containerEdits"]["deviceNodes"]]
        self.assertTrue(emitted)
        for node in emitted:
            self.assertEqual(node["fileMode"], 0o660)
            self.assertEqual(node["uid"], 0)
            self.assertEqual(node["gid"], 44)

    def test_permissions_applied_to_secure_sibling(self):
        # The folded-in -secure node must not be left without attributes.
        perms = gen.node_permissions("fastrpc-cdsp")
        devices = gen.generate_devicenodes_cdi(
            "fastrpc-cdsp", ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"], perms)
        named = {d["name"]: d for d in devices}
        secure = [n for n in named["fastrpc-cdsp"]["containerEdits"]["deviceNodes"]
                  if n["path"].endswith("-secure")]
        self.assertEqual(len(secure), 1)
        self.assertEqual(secure[0], dict({"path": "/dev/fastrpc-cdsp-secure"}, **perms))

    def test_permissions_are_not_aliased(self):
        # Each emitted node needs its own dict; a shared one would let a later
        # mutation leak across every device in the spec.
        perms = gen.node_permissions("v4l2")
        devices = gen.generate_devicenodes_cdi("video", ["/dev/video0", "/dev/video1"], perms)
        emitted = [n for d in devices for n in d["containerEdits"]["deviceNodes"]]
        self.assertEqual(len({id(n) for n in emitted}), len(emitted))
        # Mutating the source dict must not retroactively change the output.
        perms["gid"] = 999
        self.assertTrue(all(n["gid"] == 44 for n in emitted))

    def test_permissions_omitted_when_not_requested(self):
        # Called without permissions the helper keeps the bare path-only form.
        devices = gen.generate_devicenodes_cdi("video", ["/dev/video0"])
        node = devices[0]["containerEdits"]["deviceNodes"][0]
        self.assertEqual(node, {"path": "/dev/video0"})

    def test_per_node_permissions_callable(self):
        # A callable is asked once per node, which is how each node gets the
        # group that owns it rather than one group for the whole class.
        gids = {"/dev/video0": 44, "/dev/video1": 1002}
        devices = gen.generate_devicenodes_cdi(
            "video", list(gids),
            lambda node: gen.node_permissions("v4l2", None, None, gids[node]))
        emitted = {n["path"]: n["gid"]
                   for d in devices for n in d["containerEdits"]["deviceNodes"]}
        self.assertEqual(emitted, gids)

    def test_per_node_permissions_reach_secure_sibling(self):
        # The folded-in -secure node is resolved through the same callable, so
        # it cannot inherit the wrong group from its parent.
        gids = {"/dev/fastrpc-cdsp": 1000, "/dev/fastrpc-cdsp-secure": 1002}
        devices = gen.generate_devicenodes_cdi(
            "fastrpc-cdsp", list(gids),
            lambda node: gen.node_permissions("fastrpc-cdsp", None, None, gids[node]))
        named = {d["name"]: d for d in devices}
        emitted = {n["path"]: n["gid"]
                   for n in named["fastrpc-cdsp"]["containerEdits"]["deviceNodes"]}
        self.assertEqual(emitted, gids)

    def test_filemode_parsed_as_octal(self):
        # Modes are conventionally octal, with or without a leading zero.
        self.assertEqual(gen.parse_octal_mode("660"), 0o660)
        self.assertEqual(gen.parse_octal_mode("0660"), 0o660)
        self.assertEqual(gen.parse_octal_mode("0o660"), 0o660)
        # An explicit non-octal prefix is still honoured.
        self.assertEqual(gen.parse_octal_mode("0x1b0"), 0o660)
        # '8' is not an octal digit, and neither is a bare word.
        for bad in ("668", "rw-rw----", ""):
            with self.assertRaises(gen.argparse.ArgumentTypeError):
                gen.parse_octal_mode(bad)

    def _written_nodes(self, destdir, filename):
        spec = json.loads((Path(destdir) / "run" / "cdi" / filename).read_text())
        return [n for d in spec["devices"] for n in d["containerEdits"]["deviceNodes"]]

    def test_generated_files_carry_permissions(self):
        # End to end: the synthetic nodes do not exist on disk, so the group
        # resolution falls back to the per-class default. The JSON must
        # round-trip fileMode as a decimal integer (the JSON grammar has no
        # octal literal).
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            for filename, gid in (("qualcomm-v4l2.json", 44),
                                  ("qualcomm-gpu.json", 1000),
                                  ("qualcomm-dmaheap.json", 1000),
                                  ("qualcomm-fastrpc-cdsp.json", 1000),
                                  ("qualcomm-fastrpc-adsp.json", 1000)):
                nodes = self._written_nodes(d, filename)
                self.assertTrue(nodes, "%s has no device nodes" % filename)
                for node in nodes:
                    self.assertEqual(node["fileMode"], 0o660, filename)
                    self.assertEqual(node["uid"], 0, filename)
                    self.assertEqual(node["gid"], gid, filename)

    def test_host_node_group_reaches_generated_files(self):
        # A run against nodes owned by a group that matches no built-in default
        # must emit that group, in the deviceNodes entries and additionalGids
        # alike, so the container joins the group that owns what it is given.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], node_gids=ALL_NODES_STAT), 0)
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json",
                             "qualcomm-dmaheap.json", "qualcomm-fastrpc-cdsp.json",
                             "qualcomm-fastrpc-adsp.json"):
                spec = self._spec(d, filename)
                self.assertEqual(spec["containerEdits"]["additionalGids"],
                                 [ALL_NODES_GID], filename)
                for node in self._written_nodes(d, filename):
                    self.assertEqual(node["gid"], ALL_NODES_GID, filename)

    def test_host_node_group_is_per_class(self):
        # Each class resolves its own group, so a board that puts the V4L2
        # nodes in 'video' and the GPU nodes in 'render' gets both.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], node_gids={
                "/dev/dri/renderD128": 110,
                "/dev/dri/card0": 110,
                "/dev/kgsl-3d0": 110,
                "/dev/video0": 44,
                "/dev/video1": 44,
            })
            self.assertEqual(rc, 0)
            self.assertEqual(
                self._spec(d, "qualcomm-gpu.json")["containerEdits"]["additionalGids"],
                [110])
            self.assertEqual(
                self._spec(d, "qualcomm-v4l2.json")["containerEdits"]["additionalGids"],
                [44])
            # A class whose nodes could not be stat()ed keeps its default.
            self.assertEqual(
                self._spec(d, "qualcomm-dmaheap.json")["containerEdits"]["additionalGids"],
                [1000])

    def test_mixed_group_class_grants_every_group(self):
        # A board that splits one class across groups (seen in the wild as
        # /dev/video32=44 alongside /dev/video34=1002) must get every group in
        # additionalGids, and each node must keep its own gid. Granting only
        # the first would leave the differing node unopenable.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], node_gids={
                "/dev/video0": 44,
                "/dev/video1": 1002,
            })
            self.assertEqual(rc, 0)
            spec = self._spec(d, "qualcomm-v4l2.json")
            self.assertEqual(spec["containerEdits"]["additionalGids"], [44, 1002])
            bypath = {n["path"]: n["gid"]
                      for n in self._written_nodes(d, "qualcomm-v4l2.json")}
            self.assertEqual(bypath, {"/dev/video0": 44, "/dev/video1": 1002})

    def test_mixed_group_gid_override_collapses_to_one(self):
        # --gid replaces every probed group, so the spec goes back to granting
        # exactly one even on a board whose nodes disagree.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-g", "1001"], node_gids={
                "/dev/video0": 44,
                "/dev/video1": 1002,
            })
            self.assertEqual(rc, 0)
            spec = self._spec(d, "qualcomm-v4l2.json")
            self.assertEqual(spec["containerEdits"]["additionalGids"], [1001])
            for node in self._written_nodes(d, "qualcomm-v4l2.json"):
                self.assertEqual(node["gid"], 1001)

    def test_cli_overrides_reach_generated_files(self):
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-m", "666", "-u", "1000", "-g", "1001"])
            self.assertEqual(rc, 0)
            # -g overrides the per-class gid for every class, v4l2 included.
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json"):
                for node in self._written_nodes(d, filename):
                    self.assertEqual(node["fileMode"], 0o666, filename)
                    self.assertEqual(node["uid"], 1000, filename)
                    self.assertEqual(node["gid"], 1001, filename)

    def test_legacy_monolithic_cdi_removed(self):
        # An old single-file qualcomm.json left in /run/cdi must be removed so it
        # cannot define stale/conflicting devices alongside the per-class files.
        with tempfile.TemporaryDirectory() as d:
            cdi_dir = Path(d) / "run" / "cdi"
            cdi_dir.mkdir(parents=True)
            legacy = cdi_dir / "qualcomm.json"
            legacy.write_text('{"cdiVersion": "0.6.0", "kind": "qualcomm.com/legacy"}')

            rc = run_generator(["-d", d])

            self.assertEqual(rc, 0)
            self.assertFalse(legacy.exists(), "legacy monolithic CDI was not removed")
            # The per-class files use a different name and must still be written.
            self.assertTrue((cdi_dir / "qualcomm-gpu.json").is_file())

    def test_legacy_monolithic_cdi_preserved_on_dry_run(self):
        # A dry run reports what it would remove but must not touch the file.
        with tempfile.TemporaryDirectory() as d:
            cdi_dir = Path(d) / "run" / "cdi"
            cdi_dir.mkdir(parents=True)
            legacy = cdi_dir / "qualcomm.json"
            legacy.write_text("{}")

            rc = run_generator(["-d", d, "-n"])

            self.assertEqual(rc, 0)
            self.assertTrue(legacy.exists(), "dry run must not remove the legacy CDI")

    def _spec(self, destdir, filename):
        return json.loads((Path(destdir) / "run" / "cdi" / filename).read_text())

    def test_no_hook_script_is_written(self):
        # The generator must not write anything outside /run/cdi: the platform
        # filesystem is read-only on target, and a chmod hook would change the
        # host device node permissions, which the CDI attributes replace.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            self.assertFalse((Path(d) / "bin").exists(),
                             "generator created a bin/ directory on the host")
            written = sorted(p.relative_to(d).as_posix()
                             for p in Path(d).rglob("*") if p.is_file())
            self.assertTrue(written)
            for path in written:
                self.assertTrue(path.startswith("run/cdi/"),
                                "unexpected file written outside /run/cdi: %s" % path)

    def test_generated_files_have_no_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            for filename in ("qualcomm-gpu.json", "qualcomm-v4l2.json",
                             "qualcomm-dmaheap.json", "qualcomm-fastrpc-cdsp.json",
                             "qualcomm-fastrpc-adsp.json"):
                spec = self._spec(d, filename)
                self.assertNotIn("hooks", spec["containerEdits"], filename)
                # Nothing anywhere in the spec may reference the old hook.
                self.assertNotIn("vendorhook", json.dumps(spec), filename)

    def test_generated_files_carry_additional_gids(self):
        # Each class grants the group that owns its nodes, so the container can
        # open the 0660 node without any host-side permission change.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            for filename, gid in (("qualcomm-v4l2.json", 44),
                                  ("qualcomm-gpu.json", 1000),
                                  ("qualcomm-dmaheap.json", 1000),
                                  ("qualcomm-fastrpc-cdsp.json", 1000),
                                  ("qualcomm-fastrpc-adsp.json", 1000)):
                spec = self._spec(d, filename)
                self.assertEqual(spec["containerEdits"]["additionalGids"], [gid],
                                 filename)

    def test_gid_override_reaches_additional_gids(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-g", "1001"]), 0)
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json"):
                self.assertEqual(
                    self._spec(d, filename)["containerEdits"]["additionalGids"],
                    [1001], filename)

    def test_gid_override_beats_host_node_group(self):
        # --gid has to win over a group that was really read off a node, not
        # just over the per-class fallback.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-g", "1001"], node_gids=ALL_NODES_STAT)
            self.assertEqual(rc, 0)
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json"):
                spec = self._spec(d, filename)
                self.assertEqual(spec["containerEdits"]["additionalGids"],
                                 [1001], filename)
                for node in self._written_nodes(d, filename):
                    self.assertEqual(node["gid"], 1001, filename)

    def test_classes_still_scopes_generated_files(self):
        # --classes selects which CDI files are written; this used to also be
        # asserted via the hook's chmod list, which no longer exists.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "--classes", "fastrpc-cdsp"]), 0)
            written = sorted(p.name for p in (Path(d) / "run" / "cdi").glob("*.json"))
            self.assertEqual(written, ["qualcomm-fastrpc-cdsp.json"])


class CameraDetectionTests(unittest.TestCase):
    """Tests for the sysfs camera probe ported from check-camx-overlay.sh."""

    def _sysfs(self, modules):
        """Build a sysfs-shaped tree: {node name: driver module dir or None}.

        Mirrors the layout the on-target script walks:
            /sys/class/video4linux/<node>/device/driver/module -> <module dir>
        A None module leaves the chain broken at 'driver', standing in for a
        built-in driver that exposes no module.
        """
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        v4l = root / "sys" / "class" / "video4linux"
        for node, module in modules.items():
            driver = v4l / node / "device" / "driver"
            driver.mkdir(parents=True)
            if module is not None:
                modpath = root / "sys" / "module" / module
                modpath.mkdir(parents=True, exist_ok=True)
                (driver / "module").symlink_to(modpath)
        return root, v4l

    def _patched_probe(self, v4l, node):
        """Run is_camera_devicenode() against a synthetic sysfs root."""
        realpath = gen.Path

        class FakePath(type(realpath())):
            # Redirect only the one sysfs path the probe builds; everything
            # else (including Path(node).name) behaves normally.
            def __new__(cls, *args):
                if args and str(args[0]) == "/sys/class/video4linux":
                    return realpath(v4l, *args[1:])
                return realpath(*args)

        with mock.patch.object(gen, "Path", FakePath):
            return gen.is_camera_devicenode(node)

    def test_camera_module_detected(self):
        # A driver module whose name carries 'camera' marks the node a camera,
        # which is exactly the substring test the shell script performs.
        root, v4l = self._sysfs({"video0": "qcom_camera_isp"})
        self.assertTrue(self._patched_probe(v4l, "/dev/video0"))

    def test_non_camera_module_rejected(self):
        # The encode/decode nodes on the same board resolve to a module that
        # does not name a camera.
        root, v4l = self._sysfs({"video32": "venus_enc"})
        self.assertFalse(self._patched_probe(v4l, "/dev/video32"))

    def test_missing_module_link_rejected(self):
        # The script's 2>/dev/null case: no module link means not a camera.
        root, v4l = self._sysfs({"video0": None})
        self.assertFalse(self._patched_probe(v4l, "/dev/video0"))

    def test_absent_node_rejected(self):
        # A node with no sysfs entry at all is not a camera either.
        root, v4l = self._sysfs({})
        self.assertFalse(self._patched_probe(v4l, "/dev/video9"))

    def test_camera_range_limits_candidates(self):
        # Only /dev/video0..31 are considered, so an encoder high up the range
        # is never probed even if its driver would match.
        probed = []

        def probe(node):
            probed.append(node)
            return True

        nodes = ["/dev/video0", "/dev/video31", "/dev/video32", "/dev/video33"]
        with mock.patch.object(gen, "is_camera_devicenode", probe):
            cameras = gen.find_camera_devicenodes(nodes)
        # video31 is in range (the range is inclusive of 31); video32+ are not.
        self.assertEqual(cameras, ["/dev/video0", "/dev/video31"])
        self.assertEqual(probed, ["/dev/video0", "/dev/video31"])

    def test_only_camera_nodes_selected(self):
        # Of the in-range nodes, only those the driver probe accepts are kept.
        nodes = ["/dev/video0", "/dev/video1", "/dev/video2"]
        cameras = {"/dev/video0", "/dev/video2"}
        with mock.patch.object(gen, "is_camera_devicenode", cameras.__contains__):
            self.assertEqual(gen.find_camera_devicenodes(nodes),
                             ["/dev/video0", "/dev/video2"])

    def test_no_cameras_yields_empty(self):
        with mock.patch.object(gen, "is_camera_devicenode", lambda node: False):
            self.assertEqual(gen.find_camera_devicenodes(["/dev/video0"]), [])


class NewClassTests(unittest.TestCase):
    """End-to-end tests for the gpu/camera/audio/display class generation."""

    def _spec(self, destdir, cdiclass):
        path = Path(destdir) / "run" / "cdi" / ("qualcomm-%s.json" % cdiclass)
        return json.loads(path.read_text())

    def _written(self, destdir):
        return sorted(p.name for p in (Path(destdir) / "run" / "cdi").glob("*.json"))

    def _devices(self, spec):
        return {d["name"]: d for d in spec["devices"]}

    def test_gpu_carries_render_card_and_kgsl(self):
        # The GPU class exposes the render node, the DRM card node and the
        # KGSL node, each under its own device name, plus catch-alls.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-C", "gpu"]), 0)
            devices = self._devices(self._spec(d, "gpu"))
            self.assertIn("renderD128", devices)
            self.assertIn("card0", devices)
            self.assertIn("kgsl-3d0", devices)
            # The historical renderD:all name is kept for compatibility, and
            # gpu:all hands over the whole stack.
            self.assertIn("renderD:all", devices)
            self.assertIn("gpu:all", devices)
            allpaths = sorted(n["path"] for n in
                              devices["gpu:all"]["containerEdits"]["deviceNodes"])
            self.assertEqual(allpaths, ["/dev/dri/card0", "/dev/dri/renderD128",
                                        "/dev/kgsl-3d0"])

    def test_gpu_without_kgsl_falls_back_to_card(self):
        # On a board with no /dev/kgsl-3d0 the class is just render + card.
        nodes = dict(FAKE_NODES, **{"/dev/kgsl-3d*": []})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-C", "gpu"], nodes), 0)
            devices = self._devices(self._spec(d, "gpu"))
            self.assertIn("renderD128", devices)
            self.assertIn("card0", devices)
            self.assertNotIn("kgsl-3d0", devices)
            allpaths = sorted(n["path"] for n in
                              devices["gpu:all"]["containerEdits"]["deviceNodes"])
            self.assertEqual(allpaths, ["/dev/dri/card0", "/dev/dri/renderD128"])

    def test_gpu_catchall_defined_once(self):
        # Each device name must be unique within a spec; the per-glob calls
        # must not each contribute their own ':all'.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-C", "gpu"]), 0)
            names = [dev["name"] for dev in self._spec(d, "gpu")["devices"]]
            self.assertEqual(len(names), len(set(names)), names)
            self.assertEqual([n for n in names if n.endswith(":all")],
                             ["renderD:all", "gpu:all"])

    def test_gpu_spans_render_and_card_groups(self):
        # A render node in 'render' next to a card node in 'video' is the
        # normal case; the spec has to grant both groups.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-C", "gpu"], node_gids={
                "/dev/dri/renderD128": 110,
                "/dev/dri/card0": 44,
                "/dev/kgsl-3d0": 110,
            })
            self.assertEqual(rc, 0)
            spec = self._spec(d, "gpu")
            self.assertEqual(spec["containerEdits"]["additionalGids"], [44, 110])
            bypath = {n["path"]: n["gid"] for dev in spec["devices"]
                      for n in dev["containerEdits"]["deviceNodes"]}
            self.assertEqual(bypath["/dev/dri/renderD128"], 110)
            self.assertEqual(bypath["/dev/dri/card0"], 44)
            self.assertEqual(bypath["/dev/kgsl-3d0"], 110)

    def test_camera_requires_a_camera_driver(self):
        # No camera driver on the board means no camera spec, even though
        # /dev/video* nodes exist and the socket is present.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=FAKE_SOCKETS.values())
            self.assertEqual(rc, 0)
            self.assertNotIn("qualcomm-camera.json", self._written(d))

    def test_camera_nodes_and_socket(self):
        # With a camera driver bound, the camera class carries that node and
        # bind-mounts the camera server socket.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["camera"]],
                               cameras=["/dev/video0"])
            self.assertEqual(rc, 0)
            spec = self._spec(d, "camera")
            paths = [n["path"] for dev in spec["devices"]
                     for n in dev["containerEdits"]["deviceNodes"]]
            # Only the camera node, not the other V4L2 node on the board.
            self.assertEqual(set(paths), {"/dev/video0"})
            mounts = spec["containerEdits"]["mounts"]
            self.assertEqual([m["hostPath"] for m in mounts],
                             [FAKE_SOCKETS["camera"]])
            self.assertEqual(mounts[0]["containerPath"], FAKE_SOCKETS["camera"])
            self.assertIn("rw", mounts[0]["options"])

    def test_camera_without_socket_still_exposes_nodes(self):
        # The nodes are real even when the camera server is not running; the
        # spec then carries no mount rather than a dangling one.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], cameras=["/dev/video0"])
            self.assertEqual(rc, 0)
            spec = self._spec(d, "camera")
            self.assertNotIn("mounts", spec["containerEdits"])

    def test_v4l2_still_carries_every_node(self):
        # The camera class narrows the selection; v4l2 keeps all of them.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], cameras=["/dev/video0"])
            self.assertEqual(rc, 0)
            paths = {n["path"] for dev in self._spec(d, "v4l2")["devices"]
                     for n in dev["containerEdits"]["deviceNodes"]}
            self.assertEqual(paths, {"/dev/video0", "/dev/video1"})

    def test_audio_and_display_are_socket_mounts(self):
        # The socket-only classes pass through no device node; asking for the
        # device is what bind-mounts the socket.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["audio"],
                                                   FAKE_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            for cdiclass, socket in (("audio", FAKE_SOCKETS["audio"]),
                                     ("display", FAKE_SOCKETS["display"])):
                spec = self._spec(d, cdiclass)
                devices = self._devices(spec)
                self.assertIn(cdiclass, devices)
                self.assertIn(cdiclass + ":all", devices)
                edits = devices[cdiclass]["containerEdits"]
                self.assertNotIn("deviceNodes", edits)
                self.assertEqual([m["hostPath"] for m in edits["mounts"]], [socket])
                self.assertEqual(edits["mounts"][0]["containerPath"], socket)

    def test_absent_sockets_skip_the_class(self):
        # No socket means the server is not running, so no spec is written.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            written = self._written(d)
            self.assertNotIn("qualcomm-audio.json", written)
            self.assertNotIn("qualcomm-display.json", written)

    def test_socket_group_reaches_additional_gids(self):
        # The socket is group-owned like a device node, and the container has
        # to join that group to connect to it.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["audio"]],
                               node_gids={FAKE_SOCKETS["audio"]: 29})
            self.assertEqual(rc, 0)
            self.assertEqual(
                self._spec(d, "audio")["containerEdits"]["additionalGids"], [29])

    def test_camera_gids_span_nodes_and_socket(self):
        # The camera class reads a group from both its nodes and its socket.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["camera"]],
                               cameras=["/dev/video0"],
                               node_gids={"/dev/video0": 44,
                                          FAKE_SOCKETS["camera"]: 1002})
            self.assertEqual(rc, 0)
            self.assertEqual(
                self._spec(d, "camera")["containerEdits"]["additionalGids"],
                [44, 1002])

    def test_all_classes_written_on_a_full_board(self):
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                               cameras=["/dev/video0"])
            self.assertEqual(rc, 0)
            self.assertEqual(self._written(d), [
                "qualcomm-audio.json",
                "qualcomm-camera.json",
                "qualcomm-display.json",
                "qualcomm-dmaheap.json",
                "qualcomm-fastrpc-adsp.json",
                "qualcomm-fastrpc-cdsp.json",
                "qualcomm-gpu.json",
                "qualcomm-v4l2.json",
            ])

    def test_new_classes_are_selectable(self):
        # --classes has to accept the new names, and scope the output to them.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-C", "audio,display"],
                               sockets=FAKE_SOCKETS.values())
            self.assertEqual(rc, 0)
            self.assertEqual(self._written(d), ["qualcomm-audio.json",
                                                "qualcomm-display.json"])


@unittest.skipIf(CDI_TOOL is None,
                 "cdi tool not found (set CDI_TOOL or put 'cdi' on PATH)")
class ValidationTests(unittest.TestCase):
    """Validate generated CDI JSON with the upstream 'cdi' tool.

    'cdi validate' checks each spec against the CDI JSON schema; it does not
    require the referenced device nodes to exist, so synthetic node lists give
    deterministic results independent of the host hardware.
    """

    def _validate_dir(self, spec_dir):
        """Run 'cdi validate' against spec_dir; return (returncode, output)."""
        proc = subprocess.run(
            [CDI_TOOL, "validate", "--spec-dirs", str(spec_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout

    def _write_spec(self, spec_dir, cdiclass, spec):
        path = Path(spec_dir) / ("qualcomm-%s.json" % cdiclass)
        with open(path, "w") as f:
            json.dump(spec, f)
        return path

    def _assert_valid(self, spec_dir):
        rc, out = self._validate_dir(spec_dir)
        self.assertEqual(rc, 0, "cdi validate reported errors:\n%s" % out)

    def test_gpu_spec_validates(self):
        nodes = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
        devices = gen.generate_devicenodes_cdi("renderD", nodes,
                                               gen.node_permissions("gpu"))
        spec = gen.build_cdi_spec("gpu", devices, [], [],
                                   gen.node_additional_gids("gpu"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "gpu", spec)
            self._assert_valid(d)

    def test_v4l2_spec_validates(self):
        nodes = ["/dev/video%d" % i for i in range(5)]
        devices = gen.generate_devicenodes_cdi("video", nodes,
                                               gen.node_permissions("v4l2"))
        spec = gen.build_cdi_spec("v4l2", devices, [], [],
                                   gen.node_additional_gids("v4l2"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "v4l2", spec)
            self._assert_valid(d)

    def test_dmaheap_spec_validates(self):
        devices = gen.generate_devicenodes_cdi("dmaheap-system", ["/dev/dma_heap/system"],
                                               gen.node_permissions("dmaheap"))
        spec = gen.build_cdi_spec("dmaheap", devices, [], [],
                                   gen.node_additional_gids("dmaheap"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "dmaheap", spec)
            self._assert_valid(d)

    def test_fastrpc_spec_with_mounts_and_env_validates(self):
        nodes = ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"]
        devices = gen.generate_devicenodes_cdi("fastrpc-cdsp", nodes,
                                               gen.node_permissions("fastrpc-cdsp"))
        mounts = [{"hostPath": "/usr/share/foo/bar/1.0/aarch64/dsp/",
                   "containerPath": "/usr/share/foo/bar/1.0/aarch64/dsp/",
                   "options": ["nosuid", "ro", "bind"]}]
        env = ["MACHINE_NAME=Synthetic Board"]
        spec = gen.build_cdi_spec("fastrpc-cdsp", devices, env, mounts,
                                   gen.node_additional_gids("fastrpc-cdsp"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "fastrpc-cdsp", spec)
            self._assert_valid(d)

    def test_fastrpc_spec_without_mounts_validates(self):
        # When no DSP firmware is found the mounts key is omitted entirely.
        devices = gen.generate_devicenodes_cdi("fastrpc-adsp", ["/dev/fastrpc-adsp"])
        spec = gen.build_cdi_spec("fastrpc-adsp", devices, [], [])
        self.assertNotIn("mounts", spec["containerEdits"])
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "fastrpc-adsp", spec)
            self._assert_valid(d)

    def test_gpu_spec_with_card_and_kgsl_validates(self):
        # The multi-glob GPU class: several device names in one spec, each node
        # with its own gid, and two groups in additionalGids.
        perms = lambda node: gen.node_permissions(
            "gpu", None, None, 44 if "card" in node else 110)
        devices = (gen.generate_devicenodes_cdi("renderD", ["/dev/dri/renderD128"], perms)
                   + gen.generate_devicenodes_cdi("card", ["/dev/dri/card0"], perms,
                                                  catchall=False)
                   + gen.generate_devicenodes_cdi("kgsl-3d", ["/dev/kgsl-3d0"], perms,
                                                  catchall=False)
                   + gen.generate_devicenodes_cdi(
                       "gpu", ["/dev/dri/renderD128", "/dev/dri/card0", "/dev/kgsl-3d0"],
                       perms, pernode=False))
        spec = gen.build_cdi_spec("gpu", devices, [], [],
                                  gen.node_additional_gids("gpu", [44, 110]))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "gpu", spec)
            self._assert_valid(d)

    def test_camera_spec_with_socket_validates(self):
        devices = gen.generate_devicenodes_cdi("camera", ["/dev/video0", "/dev/video1"],
                                               gen.node_permissions("camera"))
        mounts = gen.generate_mounts_cdi(["/run/cam_server/le_camera_socket"])
        spec = gen.build_cdi_spec("camera", devices, [], mounts,
                                  gen.node_additional_gids("camera", [44, 1002]))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "camera", spec)
            self._assert_valid(d)

    def test_socket_only_spec_validates(self):
        # A device whose containerEdits carry only mounts and no deviceNodes is
        # a shape the other classes never produce; confirm the schema accepts it.
        for cdiclass, socket in (("audio", "/run/user/1000/pulse/native"),
                                 ("display", "/run/user/1000/wayland-0")):
            devices = gen.generate_sockets_cdi(cdiclass, [socket])
            spec = gen.build_cdi_spec(cdiclass, devices, [], [],
                                      gen.node_additional_gids(cdiclass, [1000]))
            with tempfile.TemporaryDirectory() as d:
                self._write_spec(d, cdiclass, spec)
                self._assert_valid(d)

    def test_full_generator_run_validates(self):
        # Drive main() end to end with synthetic device discovery so every class
        # is produced, then validate the whole set of written files together.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                               cameras=["/dev/video0"])

            self.assertEqual(rc, 0)
            cdi_dir = Path(d) / "run" / "cdi"
            written = sorted(p.name for p in cdi_dir.glob("*.json"))
            self.assertEqual(written, [
                "qualcomm-audio.json",
                "qualcomm-camera.json",
                "qualcomm-display.json",
                "qualcomm-dmaheap.json",
                "qualcomm-fastrpc-adsp.json",
                "qualcomm-fastrpc-cdsp.json",
                "qualcomm-gpu.json",
                "qualcomm-v4l2.json",
            ])
            self._assert_valid(cdi_dir)

    def test_malformed_spec_is_rejected(self):
        # Negative control: confirm the validator actually fails on a bad spec,
        # so a passing run above means something.
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "qualcomm-bad.json"
            with open(bad, "w") as f:
                # 'devices' must be an array; null is invalid per the schema.
                json.dump({"cdiVersion": gen.CDI_VERSION,
                           "kind": gen.CDI_VENDOR + "/bad",
                           "devices": None}, f)
            rc, out = self._validate_dir(d)
            self.assertNotEqual(rc, 0,
                                "cdi validate unexpectedly accepted a malformed spec:\n%s" % out)


if __name__ == "__main__":
    unittest.main()
