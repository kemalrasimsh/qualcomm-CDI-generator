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

import fnmatch
import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
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
    "/dev/fastrpc-gdsp*": ["/dev/fastrpc-gdsp0", "/dev/fastrpc-gdsp1"],
}

# Every CDI file a fully-populated synthetic host writes, which is one per
# class. The gdsp file is here too: FAKE_NODES presents the gdsp nodes an
# SA8775P-class board has, the same board the three DSP firmware directories
# below belong to.
FAKE_CDI_FILES = ["qualcomm-audio.json", "qualcomm-camera.json",
                  "qualcomm-display.json", "qualcomm-dmaheap.json",
                  "qualcomm-fastrpc-adsp.json", "qualcomm-fastrpc-cdsp.json",
                  "qualcomm-fastrpc-gdsp.json", "qualcomm-gpu.json",
                  "qualcomm-v4l2.json"]

# The devicetree node holding the board name, which the fastrpc specs mount,
# and the name a run presents there when a test asks for one. The generator
# also reads the node to log the board it believes it is on.
DEVICETREE_MODEL = "/sys/firmware/devicetree/base/model"
DEVICE_MODEL_MOUNTPOINT = "/run/device-model"
FAKE_MACHINE = "Synthetic Board"

# The hexagon-dsp configuration the firmware directory is resolved from: the
# conf.d the qualcomm-linux images install, a board it names, and the directory
# its DSP_LIBRARY_PATH resolves to under the directory holding that conf.d.
QCOM_CONF_DIR = "/usr/share/qcom/conf.d"
HEXAGON_CONF_DIR = "/usr/share/hexagon-dsp/conf.d"
QCOM_CONF_FILE = QCOM_CONF_DIR + "/hexagon-dsp-binaries-thundercomm-rb3gen2.yaml"
RB3GEN2_MACHINE = "Qualcomm Technologies, Inc. Robotics RB3gen2"
RB3GEN2_LIBRARY_PATH = "qcm6490/Thundercomm/RB3gen2/dsp"
RB3GEN2_DSP = "/usr/share/qcom/" + RB3GEN2_LIBRARY_PATH
SA8775P_DSP = "/usr/share/qcom/sa8775p/Qualcomm/SA8775P-RIDE/dsp"

# One board's configuration file, as the image ships it: the SPDX header and
# the schema annotation a parser has to pass over, and one machine entry.
FAKE_CONF_FILE = """# SPDX-License-Identifier: MIT
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# yaml-language-server: $schema=schema.json
machines:
  %s:
    DSP_LIBRARY_PATH: %s
""" % (RB3GEN2_MACHINE, RB3GEN2_LIBRARY_PATH)

# Where the firmware files are presented inside the container, whatever
# board-specific directory the host kept them in.
CONTAINER_DSP_DIR = "/usr/share/hexagon-dsp"

# Host sockets the socket-backed classes look for. Keyed by class so a test can
# present or withhold each one; withheld means the server is not running. These
# are the per-user runtime paths, i.e. the fallback pattern of each class rather
# than the preferred system-wide one, which is the common session layout.
FAKE_SOCKETS = {
    "audio": "/run/user/1000/pulse/native",
    "camera": "/run/cam_server/le_camera_socket",
    "display": "/run/user/1000/wayland-0",
}

# The system-wide alternatives, matched by the first pattern of each class.
FAKE_SYSTEM_SOCKETS = {
    "audio": "/run/pulse/native",
    "display": "/run/wayland-0",
}

# Where each class's socket is presented inside the container: the system-wide
# /run location, for every host layout above. A host serving the socket from
# /run is mounted where it was found, and a per-user one is lifted up to the
# same path, so the container resolves one path either way.
CONTAINER_SOCKETS = {
    "audio": "/run/pulse/native",
    "camera": "/run/cam_server/le_camera_socket",
    "display": "/run/wayland-0",
}


def path_matches(pattern, path):
    """Report whether one path is what glob(pattern) would return.

    fnmatch alone would let a '*' span '/', so '/run/user/*/pulse/native' would
    wrongly match a deeper path. Compare segment by segment instead, which is
    how glob treats a wildcard.

    This deliberately re-implements what the generator's glob_matches() does
    rather than calling it: it backs the fake glob the end-to-end tests run
    against, so sharing the implementation would make a bug in glob_matches
    produce a matching bug in the fixture and pass unnoticed.
    """
    patternparts = pattern.split("/")
    pathparts = path.split("/")
    return len(patternparts) == len(pathparts) and all(
        fnmatch.fnmatchcase(pathpart, patternpart)
        for patternpart, pathpart in zip(patternparts, pathparts))


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
                  sockets=(), devicetree=None, conffiles=None, boardfiles=(),
                  symlinks=None):
    """Run main() with synthetic device discovery into a fresh argv.

    find_devicenodes is replaced with controlled node lists and the direct
    glob.glob() devicetree probe is served from the devicetree argument, so the
    run does not depend on the host hardware. node_gids maps node paths to the
    group to report for them; nodes left out of it stat as absent, so the
    generator falls back to its per-class default group.

    sockets lists the host sockets that exist for this run (paths, as in
    FAKE_SOCKETS.values()); any socket not listed is reported absent, which is
    what the generator sees when the host-side server is not running.

    devicetree is the board name the host's devicetree model node holds; the
    default None presents a host with no model node at all. The fake node is
    read back NUL terminated, as the real one is.

    conffiles maps a hexagon-dsp configuration path to its contents, which is
    what the firmware directory is resolved from: the directories holding those
    paths are the conf.d this host appears to have, and nothing else is. The
    default None presents an image carrying no configuration at all, so the run
    falls through to the symlinks and then to the glob.

    symlinks maps a path under /usr/lib/dsp to the real path it resolves to,
    standing in for what the rpcd services leave there. Paths not listed
    resolve to themselves, as a real file does.

    boardfiles lists the firmware files that exist under /usr/share, i.e. in
    the board directories the configuration or the symlinks resolve to. Every
    /usr/share glob is served from this list and never from the real
    filesystem, so a run on a real Qualcomm host does not pick up that board's
    firmware.

    Returns main()'s exit code.
    """
    real_find = gen.find_devicenodes

    def fake_find(deviceglob):
        # Controlled lists for known probes; fall back to the real glob for
        # anything unexpected so a typo surfaces as a test error.
        return list(fake_nodes[deviceglob]) if deviceglob in fake_nodes \
            else real_find(deviceglob)

    real_glob = gen.glob.glob

    socketset = set(sockets)
    socketpatterns = {pattern for patterns in gen.CLASS_SOCKETS.values()
                      for pattern in patterns}

    boardfileset = set(boardfiles)

    def fake_glob(pattern, *args, **kwargs):
        if pattern == DEVICETREE_MODEL:
            return [DEVICETREE_MODEL] if devicetree is not None else []
        # Serve the socket patterns from the presented set, so find_sockets()
        # runs its real pattern-ordering/fallback logic against a controlled
        # host layout instead of being stubbed out.
        if pattern in socketpatterns:
            return [path for path in socketset if path_matches(pattern, path)]
        # The board firmware directories are absolute paths the generator
        # resolves from the SoC id and the board name, so they are globbed
        # directly rather than through find_devicenodes. Serve them from the
        # presented files: the real /usr/share of a Qualcomm host must not
        # reach a test.
        if pattern.startswith("/usr/share/"):
            # The conf.d listings come from the presented configuration, and
            # every other /usr/share glob from the presented firmware files.
            candidates = set(conffiles or ()) | boardfileset
            return sorted(path for path in candidates
                          if path_matches(pattern, path))
        if pattern == gen.DSP_SYMLINK_GLOB:
            return sorted(symlinks or ())
        return real_glob(pattern, *args, **kwargs)

    real_open = open

    def fake_open(path, *args, **kwargs):
        # The generator reads the model node directly; serve the presented
        # board name and leave every other open (the spec files it writes)
        # alone. A run presenting no board name reports the node absent, as a
        # host with no devicetree does, rather than serving an empty read: the
        # read is no longer gated on the glob, so the OSError is what tells the
        # generator there is no name to pass on.
        if path == DEVICETREE_MODEL and devicetree is None:
            raise FileNotFoundError(2, "No such file or directory", path)
        if path == DEVICETREE_MODEL:
            return io.StringIO(devicetree + "\u0000")
        # The hexagon-dsp configuration the firmware directory is resolved
        # from, served from the presented set so the real conf.d of a
        # Qualcomm host cannot decide what a test resolves.
        if path in (conffiles or {}):
            return io.StringIO(conffiles[path])
        if str(path).startswith(tuple(gen.DSP_CONF_DIRS)):
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, *args, **kwargs)

    confdirs = {str(PurePosixPath(path).parent) for path in (conffiles or {})}
    boarddirs = {str(PurePosixPath(path).parent.parent)
                 for path in boardfileset} | {
                     str(PurePosixPath(path).parent.parent.parent)
                     for path in boardfileset}
    real_isdir = os.path.isdir

    def fake_isdir(path):
        # The conf.d directories and the board firmware directories exist
        # exactly as the run presented them; anything else is the real tree
        # (the temporary output directory, above all).
        key = os.fspath(path)
        if key in confdirs or key in boarddirs:
            return True
        if key.startswith("/usr/share/"):
            return False
        return real_isdir(path)

    def fake_realpath(path, *args, **kwargs):
        return (symlinks or {}).get(os.fspath(path), os.fspath(path))

    real_exists = os.path.exists

    def fake_exists(path):
        key = os.fspath(path)
        if key in boardfileset or key in (symlinks or {}).values():
            return True
        if key.startswith("/usr/share/"):
            return False
        return real_exists(path)

    with mock.patch.object(gen, "find_devicenodes", fake_find), \
            mock.patch.object(gen.glob, "glob", fake_glob), \
            mock.patch("builtins.open", fake_open), \
            mock.patch.object(gen.os.path, "isdir", fake_isdir), \
            mock.patch.object(gen.os.path, "realpath", fake_realpath), \
            mock.patch.object(gen.os.path, "exists", fake_exists), \
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

    # Every field the CDI Spec struct declares, and nothing else. The runtimes
    # unmarshal these files into that struct with unknown fields disallowed and
    # reject the whole spec on the first key they do not know, which costs the
    # container every device in the class. The JSON schema does not set
    # 'additionalProperties: false' and the runtimes do not consult it anyway,
    # so nothing else catches a stray field.
    #
    # https://github.com/cncf-tags/container-device-interface/blob/main/specs-go/config.go
    CDI_SPEC_FIELDS = {"cdiVersion", "kind", "annotations", "devices",
                       "containerEdits"}
    CDI_DEVICE_FIELDS = {"name", "annotations", "containerEdits"}

    def _assert_fields_known(self, spec):
        self.assertLessEqual(set(spec), self.CDI_SPEC_FIELDS,
                             "spec carries fields no runtime will accept: %s"
                             % sorted(set(spec) - self.CDI_SPEC_FIELDS))
        for device in spec["devices"]:
            self.assertLessEqual(
                set(device), self.CDI_DEVICE_FIELDS,
                "device %r carries fields no runtime will accept: %s"
                % (device.get("name"),
                   sorted(set(device) - self.CDI_DEVICE_FIELDS)))

    def test_spec_carries_no_field_the_runtimes_reject(self):
        # Exercised on a spec with something in every optional field, so a
        # stray key cannot hide in a branch this does not reach.
        nodes = ["/dev/dri/renderD128"]
        spec = gen.build_cdi_spec(
            "gpu", gen.generate_devicenodes_cdi("renderD", nodes),
            ["MACHINE_NAME=" + FAKE_MACHINE],
            gen.generate_mounts_cdi(["/run/user/1000/pulse/native"]),
            gen.node_additional_gids("gpu"))
        self._assert_fields_known(spec)

    def test_every_class_spec_carries_only_known_fields(self):
        for cdiclass in gen.known_classes:
            with self.subTest(cdiclass=cdiclass):
                self._assert_fields_known(gen.build_cdi_spec(cdiclass, [], [], []))

    def test_generated_note_lives_in_the_cdi_annotations_field(self):
        # The note rides in the spec's own 'annotations' map, the only field
        # CDI provides for it. An underscore-prefixed spelling of our own is an
        # unknown field and fails the whole spec to unmarshal.
        spec = gen.build_cdi_spec("gpu", [], [], [])
        self.assertNotIn("_annotations", spec)
        self.assertEqual(spec["annotations"], gen.spec_annotations())
        self.assertIn(gen.CDI_VENDOR + "/generated-by", spec["annotations"])

    def test_annotations_sit_above_the_devices(self):
        # The note stands in for the header comment JSON cannot carry, so it
        # has to serialise at the top of the file rather than wherever a dict
        # happens to put it. This is also the order the Spec struct declares.
        keys = list(gen.build_cdi_spec("gpu", [], [], []))
        self.assertEqual(keys[:3], ["cdiVersion", "kind", "annotations"])
        self.assertLess(keys.index("annotations"), keys.index("devices"))

    def test_annotation_keys_are_vendor_qualified(self):
        # Qualified so they read like the 'kind' they sit next to and cannot
        # collide with another tool's metadata on the same spec.
        for key in gen.spec_annotations():
            self.assertTrue(key.startswith(gen.CDI_VENDOR + "/"), key)

    def test_annotations_version_is_supported(self):
        # 'annotations' was added in CDI 0.6.0; emitting it under an older
        # version would make the spec fail validation.
        self.assertGreaterEqual(
            tuple(int(p) for p in gen.CDI_VERSION.split(".")), (0, 6, 0))

    def test_no_hooks_are_emitted(self):
        # Access is granted through additionalGids alone; the spec must never
        # ask the runtime to run a host-side hook script.
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
        # /dev/video32 in 'video' and a node alongside it in a vendor group.
        # Each node keeps the group that actually owns it rather than being
        # collapsed onto one winner.
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

    def test_additional_gids_match_the_class_fallback(self):
        # Every class has a group to fall back on when nothing could be read
        # off its paths, and that group is what the container joins.
        for cdiclass in gen.known_classes:
            self.assertEqual(
                gen.node_additional_gids(cdiclass),
                [gen.CLASS_GID.get(cdiclass, gen.DEFAULT_GID)], cdiclass)

    def test_spec_version_supports_additional_gids(self):
        # additionalGids was introduced in CDI 0.7.0; emitting it under an
        # older version makes the spec fail validation.
        self.assertGreaterEqual(
            tuple(int(p) for p in gen.CDI_VERSION.split(".")), (0, 7, 0))

    def test_mounts_attached_when_given(self):
        # build_cdi_spec attaches whatever mounts the caller passes; main()
        # decides what belongs to each class (DSP firmware for fastrpc,
        # nothing for the rest).
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

    def test_env_attached_when_given(self):
        # build_cdi_spec attaches whatever env entries the caller passes, the
        # way it does the mounts; generate_enventries_cdi() is what decides
        # which class is given any.
        env = ["MACHINE_NAME=" + FAKE_MACHINE]
        for cdiclass in gen.known_classes + ["brandnew"]:
            spec = gen.build_cdi_spec(cdiclass, [], env, [])
            self.assertEqual(spec["containerEdits"]["env"], env, cdiclass)

    def test_env_omitted_when_empty(self):
        # A run with no board name must not leave a bare "env": [] behind, the
        # way a class with no mounts leaves no "mounts": [].
        for env in ([], None):
            spec = gen.build_cdi_spec("gpu", [], env, [])
            self.assertNotIn("env", spec["containerEdits"], repr(env))

    def test_env_is_the_first_container_edit(self):
        # The order the ContainerEdits struct declares the fields in, so the
        # variable a reader is after is at the top of the map rather than below
        # whichever other field happens to be there.
        mounts = [{"hostPath": "/x", "containerPath": "/x", "options": ["ro", "bind"]}]
        spec = gen.build_cdi_spec("fastrpc-cdsp", [],
                                  ["MACHINE_NAME=" + FAKE_MACHINE], mounts,
                                  [999])
        self.assertEqual(list(spec["containerEdits"]),
                         ["env", "additionalGids", "mounts"])

    def test_only_the_fastrpc_classes_set_the_board_name(self):
        # MACHINE_NAME is what the FastRPC stack resolves its Hexagon binary
        # paths against, so the two fastrpc classes set it. No other class
        # resolves a path against a board name, and a variable in their spec
        # would be exported into every container asking for a GPU or a camera.
        for cdiclass in gen.known_classes + ["brandnew"]:
            env = gen.generate_enventries_cdi(cdiclass, FAKE_MACHINE)
            if cdiclass in gen.MACHINE_NAME_CLASSES:
                self.assertEqual(env, ["MACHINE_NAME=" + FAKE_MACHINE],
                                 cdiclass)
            else:
                self.assertEqual(env, [], cdiclass)

    def test_the_fastrpc_classes_are_the_ones_carrying_the_mount(self):
        # The variable and the mounted model node are two ways to the same
        # board name, so the classes handed one are the classes handed the
        # other.
        self.assertEqual(sorted(gen.MACHINE_NAME_CLASSES),
                         ["fastrpc-adsp", "fastrpc-cdsp", "fastrpc-gdsp"])
        for cdiclass in gen.MACHINE_NAME_CLASSES:
            self.assertIn(cdiclass, gen.known_classes)

    def test_the_board_name_is_one_key_equals_value_entry(self):
        # CDI env entries are "KEY=value" strings, and a board name holding
        # spaces and a comma is carried whole rather than split or quoted.
        board = "Qualcomm Technologies, Inc. Robotics RB3gen2"
        entry, = gen.generate_enventries_cdi("fastrpc-cdsp", board)
        key, sep, value = entry.partition("=")
        self.assertEqual((key, sep), (gen.MACHINE_NAME_ENV, "="))
        self.assertEqual(value, board)

    def test_no_variable_is_set_without_a_board_name(self):
        # A host whose model node is missing or unreadable has no name to pass
        # on, and an empty MACHINE_NAME is worse than none: a client would
        # resolve its firmware paths against it.
        for cdiclass in gen.known_classes:
            self.assertEqual(gen.generate_enventries_cdi(cdiclass, None), [],
                             cdiclass)

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

    def test_vendor_qualified_node_is_named_after_its_vendor(self):
        # /dev/dma_heap/qcom,system is the vendor's spelling of the plain
        # 'system' heap. Naming it after the vendor says which heap it is,
        # where a position in the glob ('dmaheap-system0') does not and moves
        # as soon as a node appears or goes away.
        devices = gen.generate_devicenodes_cdi(
            "dmaheap-system",
            ["/dev/dma_heap/system", "/dev/dma_heap/qcom,system"])
        self.assertEqual(
            [(d["name"], [n["path"] for n in d["containerEdits"]["deviceNodes"]])
             for d in devices],
            [("dmaheap-system-qcom", ["/dev/dma_heap/qcom,system"]),
             ("dmaheap-system", ["/dev/dma_heap/system"]),
             ("dmaheap-system:all", ["/dev/dma_heap/qcom,system",
                                     "/dev/dma_heap/system"])])

    def test_plain_node_keeps_the_bare_nickname_beside_a_vendor_one(self):
        # The vendor-qualified node is named by its vendor, so it needs no
        # index and leaves the plain node the bare nickname it has on a board
        # that carries only that one.
        names = [d["name"] for d in gen.generate_devicenodes_cdi(
            "dmaheap-system",
            ["/dev/dma_heap/system", "/dev/dma_heap/qcom,system"])]
        self.assertIn("dmaheap-system", names)
        self.assertNotIn("dmaheap-system0", names)
        self.assertNotIn("dmaheap-system1", names)

    def test_a_vendor_node_alone_is_still_named_after_the_vendor(self):
        # A board with only the vendor spelling gets the vendor name, not the
        # bare nickname: which heap it is stays visible either way.
        names = [d["name"] for d in gen.generate_devicenodes_cdi(
            "dmaheap-system", ["/dev/dma_heap/qcom,system"])]
        self.assertEqual(names, ["dmaheap-system-qcom", "dmaheap-system:all"])

    def test_two_vendor_nodes_get_distinct_names(self):
        names = [d["name"] for d in gen.generate_devicenodes_cdi(
            "dmaheap-system",
            ["/dev/dma_heap/qcom,system", "/dev/dma_heap/acme,system"])]
        self.assertEqual(names, ["dmaheap-system-acme", "dmaheap-system-qcom",
                                 "dmaheap-system:all"])

    def test_vendor_node_keeps_the_index_in_its_name(self):
        # A vendor spelling that also carries an index keeps both, so two of
        # them cannot collapse onto one name.
        names = [d["name"] for d in gen.generate_devicenodes_cdi(
            "dmaheap-system",
            ["/dev/dma_heap/qcom,system0", "/dev/dma_heap/qcom,system1"])]
        self.assertEqual(names, ["dmaheap-system-qcom0", "dmaheap-system-qcom1",
                                 "dmaheap-system:all"])

    def test_a_comma_is_only_a_vendor_when_something_precedes_it(self):
        # ',system' names no vendor; such a node falls back to the index rules
        # rather than producing a trailing '-'.
        names = [d["name"] for d in gen.generate_devicenodes_cdi(
            "dmaheap-system", ["/dev/dma_heap/,system"])]
        self.assertEqual(names, ["dmaheap-system", "dmaheap-system:all"])

    def test_render_catchall_drops_the_node_prefix(self):
        # The render nodes' catch-all is named after the node class, not the
        # renderD prefix the nodes themselves are spelled with.
        devices = gen.generate_devicenodes_cdi(
            "render", ["/dev/dri/renderD128"], pernode=False)
        self.assertEqual([d["name"] for d in devices], ["render:all"])

    def test_indexed_secure_sibling_folded_into_its_parent(self):
        # A multi-DSP target's /dev/fastrpc-cdsp1-secure rides on
        # /dev/fastrpc-cdsp1, not on the unindexed node and not nowhere: it is
        # suppressed as a top-level entry because its parent is present, so
        # failing to fold it in loses the node from the spec entirely.
        nodes = ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure",
                 "/dev/fastrpc-cdsp1", "/dev/fastrpc-cdsp1-secure"]
        devices = gen.generate_devicenodes_cdi("fastrpc-cdsp", nodes)
        named = {d["name"]: [n["path"]
                             for n in d["containerEdits"]["deviceNodes"]]
                 for d in devices}
        self.assertEqual(named["fastrpc-cdsp1"],
                         ["/dev/fastrpc-cdsp1", "/dev/fastrpc-cdsp1-secure"])
        self.assertEqual(named["fastrpc-cdsp0"],
                         ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"])
        # Every node of the class reaches the catch-all, secure ones included.
        self.assertEqual(sorted(named["fastrpc-cdsp:all"]), sorted(nodes))

    def test_the_gdsp_nodes_keep_the_index_the_host_spells(self):
        # The gdsp nodes are numbered on the host where the cdsp ones start
        # unindexed (/dev/fastrpc-gdsp0 next to /dev/fastrpc-gdsp1, as
        # gdsprpcd and gdsp1rpcd serve them), so the device names come from
        # those indexes and the -secure nodes ride on their own parent.
        nodes = ["/dev/fastrpc-gdsp0", "/dev/fastrpc-gdsp0-secure",
                 "/dev/fastrpc-gdsp1", "/dev/fastrpc-gdsp1-secure"]
        devices = gen.generate_devicenodes_cdi("fastrpc-gdsp", nodes)
        named = {d["name"]: [n["path"]
                             for n in d["containerEdits"]["deviceNodes"]]
                 for d in devices}
        self.assertEqual(named["fastrpc-gdsp0"],
                         ["/dev/fastrpc-gdsp0", "/dev/fastrpc-gdsp0-secure"])
        self.assertEqual(named["fastrpc-gdsp1"],
                         ["/dev/fastrpc-gdsp1", "/dev/fastrpc-gdsp1-secure"])
        self.assertEqual(sorted(named["fastrpc-gdsp:all"]), sorted(nodes))

    def test_no_node_is_dropped_from_the_spec(self):
        # is_sibling() suppresses a -secure node whose parent is present and
        # node_paths() places it; the two must stay exact inverses, for the
        # indexed nodes and the orphaned -secure node alike.
        for nodes in (["/dev/fastrpc-adsp", "/dev/fastrpc-adsp-secure"],
                      ["/dev/fastrpc-adsp1", "/dev/fastrpc-adsp1-secure"],
                      ["/dev/fastrpc-adsp1-secure"],
                      ["/dev/fastrpc-adsp", "/dev/fastrpc-adsp1",
                       "/dev/fastrpc-adsp1-secure"]):
            devices = gen.generate_devicenodes_cdi("fastrpc-adsp", nodes)
            emitted = {n["path"] for d in devices
                       for n in d["containerEdits"]["deviceNodes"]}
            self.assertEqual(emitted, set(nodes), nodes)

    def test_unknown_class_falls_back_to_default_gid(self):
        self.assertEqual(gen.node_additional_gids("brandnew"), [gen.DEFAULT_GID])

    def test_device_nodes_carry_only_the_path(self):
        # Access is granted once per spec through additionalGids, so a node
        # entry names the path and nothing else: the runtime fills in the type,
        # major/minor and permissions from the host node it resolves.
        devices = gen.generate_devicenodes_cdi("video", ["/dev/video0", "/dev/video1"])
        emitted = [n for d in devices for n in d["containerEdits"]["deviceNodes"]]
        self.assertTrue(emitted)
        for node in emitted:
            self.assertEqual(list(node), ["path"])

    def test_secure_sibling_carries_only_the_path(self):
        # The -secure node folded into its parent entry is emitted the same way.
        devices = gen.generate_devicenodes_cdi(
            "fastrpc-cdsp", ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"])
        named = {d["name"]: d for d in devices}
        secure = [n for n in named["fastrpc-cdsp"]["containerEdits"]["deviceNodes"]
                  if n["path"].endswith("-secure")]
        self.assertEqual(secure, [{"path": "/dev/fastrpc-cdsp-secure"}])

    def test_node_entries_are_not_aliased(self):
        # Each emitted node needs its own dict; a shared one would let a later
        # mutation leak across every device in the spec.
        devices = gen.generate_devicenodes_cdi("video", ["/dev/video0", "/dev/video1"])
        emitted = [n for d in devices for n in d["containerEdits"]["deviceNodes"]]
        self.assertEqual(len({id(n) for n in emitted}), len(emitted))

    def _written_nodes(self, destdir, filename):
        spec = json.loads((Path(destdir) / "run" / "cdi" / filename).read_text())
        return [n for d in spec["devices"] for n in d["containerEdits"]["deviceNodes"]]

    def test_generated_files_carry_no_node_permissions(self):
        # End to end: the written specs describe access through additionalGids
        # only, so no deviceNodes entry may carry ownership or a mode.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json",
                             "qualcomm-dmaheap.json", "qualcomm-fastrpc-cdsp.json",
                             "qualcomm-fastrpc-adsp.json",
                             "qualcomm-fastrpc-gdsp.json"):
                nodes = self._written_nodes(d, filename)
                self.assertTrue(nodes, "%s has no device nodes" % filename)
                for node in nodes:
                    self.assertEqual(list(node), ["path"], filename)

    def test_host_node_group_reaches_generated_files(self):
        # A run against nodes owned by a group that matches no built-in default
        # must emit that group, so the container joins the group that owns what
        # it is given rather than a compiled-in guess.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], node_gids=ALL_NODES_STAT), 0)
            for filename in ("qualcomm-v4l2.json", "qualcomm-gpu.json",
                             "qualcomm-dmaheap.json", "qualcomm-fastrpc-cdsp.json",
                             "qualcomm-fastrpc-adsp.json",
                             "qualcomm-fastrpc-gdsp.json"):
                spec = self._spec(d, filename)
                self.assertEqual(spec["containerEdits"]["additionalGids"],
                                 [ALL_NODES_GID], filename)

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
        # additionalGids. Granting only the first would leave the differing
        # node unreachable.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], node_gids={
                "/dev/video0": 44,
                "/dev/video1": 1002,
            })
            self.assertEqual(rc, 0)
            spec = self._spec(d, "qualcomm-v4l2.json")
            self.assertEqual(spec["containerEdits"]["additionalGids"], [44, 1002])

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

    def test_a_host_without_a_dsp_gets_no_specs_at_all(self):
        # A board with no fastrpc node is not a target these specs are for, so
        # no class is written -- not even the ones that have nothing to do with
        # the DSP, and not the directory either.
        nodsp = dict(FAKE_NODES, **{"/dev/fastrpc-cdsp*": [],
                                    "/dev/fastrpc-adsp*": [],
                                    "/dev/fastrpc-gdsp*": []})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], fake_nodes=nodsp,
                              sockets=FAKE_SOCKETS.values(),
                              devicetree=FAKE_MACHINE), 0)
            self.assertEqual([p.relative_to(d).as_posix()
                              for p in Path(d).rglob("*") if p.is_file()], [])

    def test_a_host_with_one_dsp_still_gets_every_class(self):
        # The gate is "no DSP at all": a board with the compute DSP alone, or
        # the audio one alone, is still a target and gets the full set.
        for present in ("/dev/fastrpc-cdsp*", "/dev/fastrpc-adsp*",
                        "/dev/fastrpc-gdsp*"):
            onedsp = dict(FAKE_NODES, **{"/dev/fastrpc-cdsp*": [],
                                         "/dev/fastrpc-adsp*": [],
                                         "/dev/fastrpc-gdsp*": []})
            onedsp[present] = FAKE_NODES[present]
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(
                    run_generator(["-d", d], fake_nodes=onedsp), 0)
                written = sorted(p.name for p in
                                 (Path(d) / "run" / "cdi").glob("*.json"))
                self.assertIn("qualcomm-gpu.json", written, present)
                self.assertIn("qualcomm-%s.json" % present[5:-1], written,
                              present)

    def test_a_host_without_a_dsp_still_drops_the_legacy_cdi(self):
        # Leaving the old monolithic file behind would be handing the runtime a
        # spec, which is the one thing a DSP-less host must not get.
        nodsp = dict(FAKE_NODES, **{"/dev/fastrpc-cdsp*": [],
                                    "/dev/fastrpc-adsp*": [],
                                    "/dev/fastrpc-gdsp*": []})
        with tempfile.TemporaryDirectory() as d:
            cdi_dir = Path(d) / "run" / "cdi"
            cdi_dir.mkdir(parents=True)
            legacy = cdi_dir / "qualcomm.json"
            legacy.write_text('{"cdiVersion": "0.6.0", "kind": "qualcomm.com/legacy"}')

            self.assertEqual(run_generator(["-d", d], fake_nodes=nodsp), 0)

            self.assertFalse(legacy.exists(),
                             "legacy monolithic CDI was not removed")
            self.assertEqual(sorted(p.name for p in cdi_dir.iterdir()), [])

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

    def test_written_specs_are_formatted(self):
        # The specs are read and diffed by hand on target, so they are written
        # indented and newline-terminated rather than as one long line.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], sockets=FAKE_SOCKETS.values()), 0)
            files = sorted((Path(d) / "run" / "cdi").glob("*.json"))
            self.assertTrue(files)
            for path in files:
                text = path.read_text()
                self.assertTrue(text.endswith("\n"),
                                "%s does not end with a newline" % path.name)
                self.assertNotEqual(len(text.splitlines()), 1,
                                    "%s was written as a single line" % path.name)
                # Indentation is real nesting, not a stray leading space.
                self.assertIn('\n  "cdiVersion"', text)
                # Still valid JSON, and unchanged in content by the formatting.
                self.assertEqual(json.loads(text)["kind"],
                                 "qualcomm.com/" + path.stem.split("-", 1)[1])

    def test_formatted_output_round_trips(self):
        # Formatting must not alter what the spec says: the parsed file has to
        # equal the spec build_cdi_spec() produced.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-C", "v4l2"]), 0)
            written = json.loads(
                (Path(d) / "run" / "cdi" / "qualcomm-v4l2.json").read_text())
            self.assertEqual(written, json.loads(json.dumps(written)))
            self.assertEqual(written["cdiVersion"], gen.CDI_VERSION)
            self.assertIn("devices", written)

    def test_no_hook_script_is_written(self):
        # The generator must not write anything outside /run/cdi: the platform
        # filesystem is read-only on target, and a chmod hook would change the
        # host device node permissions, which additionalGids makes unnecessary.
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
                             "qualcomm-fastrpc-adsp.json",
                             "qualcomm-fastrpc-gdsp.json"):
                spec = self._spec(d, filename)
                self.assertNotIn("hooks", spec["containerEdits"], filename)
                # Nothing anywhere in the spec may reference the old hook.
                self.assertNotIn("vendorhook", json.dumps(spec), filename)

    def test_generated_files_carry_additional_gids(self):
        # Each class grants the group that owns its nodes, so the container can
        # reach what it is given without any host-side permission change.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d]), 0)
            for filename, gid in (("qualcomm-v4l2.json", 44),
                                  ("qualcomm-gpu.json", 1000),
                                  ("qualcomm-dmaheap.json", 1000),
                                  ("qualcomm-fastrpc-cdsp.json", 1000),
                                  ("qualcomm-fastrpc-adsp.json", 1000),
                                  ("qualcomm-fastrpc-gdsp.json", 1000)):
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

    def test_classes_still_scopes_generated_files(self):
        # --classes selects which CDI files are written; this used to also be
        # asserted via the hook's chmod list, which no longer exists.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "--classes", "fastrpc-cdsp"]), 0)
            written = sorted(p.name for p in (Path(d) / "run" / "cdi").glob("*.json"))
            self.assertEqual(written, ["qualcomm-fastrpc-cdsp.json"])


class DspFirmwareFileTests(unittest.TestCase):
    """Tests for the DSP files mounted at the FastRPC lookup path.

    These run against a real directory tree rather than a stubbed glob, so the
    relative sub-path handling is exercised the way it will be on a target.
    """

    def _dspdir(self, *files):
        """Create a host DSP firmware directory holding files, and return it."""
        dspdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dspdir, ignore_errors=True)
        for name in files:
            path = dspdir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return str(dspdir) + "/"

    def _mounts(self, dspdirs, cdiclass="fastrpc-cdsp"):
        return {m["containerPath"]: m
                for m in gen.generate_dsp_files_cdi(cdiclass, dspdirs)}

    def test_shell_and_cxx_runtime_land_in_the_lookup_path(self):
        # Every file the FastRPC client opens by absolute path is mounted under
        # /usr/share/hexagon-dsp, keeping its cdsp/ sub-path and its own
        # basename.
        dspdir = self._dspdir("cdsp/fastrpc_shell_3",
                              "cdsp/fastrpc_shell_unsigned_3",
                              "cdsp/libc++.so.1",
                              "cdsp/libc++abi.so.1")
        mounts = self._mounts([dspdir])
        self.assertEqual(sorted(mounts), [
            "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
            "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_unsigned_3",
            "/usr/share/hexagon-dsp/cdsp/libc++.so.1",
            "/usr/share/hexagon-dsp/cdsp/libc++abi.so.1",
        ])
        for containerpath, mount in mounts.items():
            self.assertEqual(mount["hostPath"],
                             dspdir + containerpath[len("/usr/share/hexagon-dsp/"):])

    def test_every_cdsp_subdirectory_is_mounted(self):
        # A multi-CDSP target keeps one firmware directory per DSP, each with
        # its own shell version (SA8775P: cdsp/fastrpc_shell_3 next to
        # cdsp1/fastrpc_shell_4). Both have to reach their own sub-path.
        dspdir = self._dspdir("cdsp/fastrpc_shell_3",
                              "cdsp/fastrpc_shell_unsigned_3",
                              "cdsp/libc++.so.1",
                              "cdsp/libc++abi.so.1",
                              "cdsp1/fastrpc_shell_4",
                              "cdsp1/fastrpc_shell_unsigned_4",
                              "cdsp1/libc++.so.1",
                              "cdsp1/libc++abi.so.1")
        mounts = self._mounts([dspdir])
        self.assertEqual(sorted(mounts), [
            "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
            "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_unsigned_3",
            "/usr/share/hexagon-dsp/cdsp/libc++.so.1",
            "/usr/share/hexagon-dsp/cdsp/libc++abi.so.1",
            "/usr/share/hexagon-dsp/cdsp1/fastrpc_shell_4",
            "/usr/share/hexagon-dsp/cdsp1/fastrpc_shell_unsigned_4",
            "/usr/share/hexagon-dsp/cdsp1/libc++.so.1",
            "/usr/share/hexagon-dsp/cdsp1/libc++abi.so.1",
        ])
        # Same basename in two DSP directories stays two distinct mounts; the
        # dedup is per container path, not per file name.
        self.assertEqual(mounts["/usr/share/hexagon-dsp/cdsp1/libc++.so.1"]["hostPath"],
                         dspdir + "cdsp1/libc++.so.1")

    def test_a_cdsp1_only_directory_is_kept(self):
        # The per-DSP directories are what makes a firmware directory worth
        # resolving from too, whichever index they carry.
        dspdir = self._dspdir("cdsp1/fastrpc_shell_4")
        self.assertEqual(gen.find_dsp_firmware_dirs("fastrpc-cdsp", [dspdir]),
                         [dspdir])

    def test_files_are_mounted_read_write(self):
        # The hand-maintained specs these replace mount the shell and its
        # runtime read-write; a read-only bind here would be a regression.
        dspdir = self._dspdir("cdsp/fastrpc_shell_unsigned_3")
        for mount in gen.generate_dsp_files_cdi("fastrpc-cdsp", [dspdir]):
            self.assertIn("rw", mount["options"], mount["containerPath"])
            self.assertNotIn("ro", mount["options"], mount["containerPath"])

    def test_absent_files_are_skipped(self):
        # A target carrying only the unsigned shell gets only that; the missing
        # files must not be mounted from a path that does not exist.
        dspdir = self._dspdir("cdsp/fastrpc_shell_unsigned_3")
        self.assertEqual(sorted(self._mounts([dspdir])),
                         ["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_unsigned_3"])

    def test_no_dsp_directories_mounts_nothing(self):
        self.assertEqual(gen.generate_dsp_files_cdi("fastrpc-cdsp", []), [])

    def test_unrelated_files_are_not_mounted(self):
        # Only the listed files are mounted individually; the rest of the
        # firmware rides on the whole-directory mount. The other DSP's shell is
        # among what the cdsp class leaves alone.
        dspdir = self._dspdir("cdsp/libsomething.so", "cdsp/fastrpc_shell_3",
                              "adsp/fastrpc_shell_3")
        self.assertEqual(sorted(self._mounts([dspdir])),
                         ["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3"])

    def test_each_class_gets_the_firmware_of_its_own_dsp(self):
        # One host directory holds the firmware of both DSPs; the class decides
        # which of them is mounted, so neither spec carries the other's shell.
        dspdir = self._dspdir("cdsp/fastrpc_shell_3", "cdsp/libc++.so.1",
                              "adsp/fastrpc_shell_3", "adsp/libc++.so.1")
        self.assertEqual(sorted(self._mounts([dspdir], "fastrpc-cdsp")),
                         ["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
                          "/usr/share/hexagon-dsp/cdsp/libc++.so.1"])
        self.assertEqual(sorted(self._mounts([dspdir], "fastrpc-adsp")),
                         ["/usr/share/hexagon-dsp/adsp/fastrpc_shell_3",
                          "/usr/share/hexagon-dsp/adsp/libc++.so.1"])

    def test_every_adsp_subdirectory_is_mounted(self):
        # The adsp globs wildcard the DSP index as the cdsp ones do, so a target
        # with a second audio DSP gets both shells under their own sub-path.
        dspdir = self._dspdir("adsp/fastrpc_shell_3",
                              "adsp/fastrpc_shell_unsigned_3",
                              "adsp1/fastrpc_shell_4",
                              "adsp1/libc++abi.so.1")
        self.assertEqual(sorted(self._mounts([dspdir], "fastrpc-adsp")), [
            "/usr/share/hexagon-dsp/adsp/fastrpc_shell_3",
            "/usr/share/hexagon-dsp/adsp/fastrpc_shell_unsigned_3",
            "/usr/share/hexagon-dsp/adsp1/fastrpc_shell_4",
            "/usr/share/hexagon-dsp/adsp1/libc++abi.so.1",
        ])

    def test_a_class_with_no_firmware_globs_mounts_nothing(self):
        # Only the fastrpc classes name firmware files; any other class asking
        # for them gets none rather than the cdsp ones by accident.
        dspdir = self._dspdir("cdsp/fastrpc_shell_3", "adsp/fastrpc_shell_3")
        self.assertEqual(gen.generate_dsp_files_cdi("gpu", [dspdir]), [])

    def test_a_directory_is_resolved_per_class(self):
        # The answer is per class, not for the fastrpc classes as a group: a
        # directory holding the audio DSP firmware alone is where the adsp
        # class resolves from and is no use to the cdsp class.
        dspdir = self._dspdir("adsp/fastrpc_shell_3")
        self.assertEqual(gen.find_dsp_firmware_dirs("fastrpc-adsp", [dspdir]),
                         [dspdir])
        self.assertEqual(gen.find_dsp_firmware_dirs("fastrpc-cdsp", [dspdir]),
                         [])

    def test_first_directory_providing_a_file_wins(self):
        # The same file in two host directories would otherwise be mounted
        # twice on one container path, which is a conflicting spec.
        perboard = self._dspdir("cdsp/fastrpc_shell_3")
        lookuppath = self._dspdir("cdsp/fastrpc_shell_3", "cdsp/libc++.so.1")
        mounts = self._mounts([perboard, lookuppath])
        self.assertEqual(sorted(mounts), ["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
                                          "/usr/share/hexagon-dsp/cdsp/libc++.so.1"])
        self.assertEqual(mounts["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3"]["hostPath"],
                         perboard + "cdsp/fastrpc_shell_3")
        # The file only the second directory has is still picked up from it.
        self.assertEqual(mounts["/usr/share/hexagon-dsp/cdsp/libc++.so.1"]["hostPath"],
                         lookuppath + "cdsp/libc++.so.1")

    def test_the_container_path_is_one_fixed_location(self):
        # Whatever board-specific directory the host kept the firmware in, the
        # container sees it under one path: the per-DSP sub-path is all that is
        # carried over from where it was found.
        dspdir = self._dspdir("cdsp/fastrpc_shell_unsigned_3")
        mounts = gen.generate_dsp_files_cdi("fastrpc-cdsp", [dspdir])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [(dspdir + "cdsp/fastrpc_shell_unsigned_3",
                           CONTAINER_DSP_DIR + "/cdsp/fastrpc_shell_unsigned_3")])

    def test_the_first_directory_with_the_firmware_wins(self):
        # Where a board answers with more than one candidate -- several
        # symlinks resolving to different directories -- the first holding
        # this class's firmware is the one used, and the ones holding no
        # Hexagon shell are passed over.
        empty = self._dspdir()
        other = self._dspdir("cdsp/some-other-firmware.so")
        booted = self._dspdir("cdsp/fastrpc_shell_3", "cdsp/libc++.so.1")
        self.assertEqual(
            gen.find_dsp_firmware_dirs("fastrpc-cdsp",
                                       [empty, other, booted]),
            [booted])

    def test_directories_without_fastrpc_files_are_not_in_the_spec(self):
        # End to end: the other board's firmware directory reaches no spec.
        booted = self._dspdir("cdsp/fastrpc_shell_3")
        otherboard = self._dspdir("cdsp/some-other-firmware.so")
        symlinks = {"/usr/lib/dsp/cdsp/fastrpc_shell_3":
                    booted + "cdsp/fastrpc_shell_3"}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], symlinks=symlinks), 0)
            spec = json.loads((Path(d) / "run" / "cdi"
                               / "qualcomm-fastrpc-cdsp.json").read_text())
            mounts = {(m["hostPath"], m["containerPath"])
                      for m in spec["containerEdits"]["mounts"]}
            self.assertIn((booted + "cdsp/fastrpc_shell_3",
                           "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3"), mounts)
            self.assertFalse([host for host, _ in mounts
                              if host.startswith(otherboard)])

    def test_fastrpc_cdsp_spec_carries_the_file_mounts(self):
        # End to end: the cdsp*/ file mounts reach the fastrpc-cdsp spec, and
        # no spec outside the fastrpc classes gets file mounts at all.
        dspdir = self._dspdir("cdsp/fastrpc_shell_unsigned_3",
                              "cdsp/libc++.so.1")
        symlinks = {"/usr/lib/dsp/cdsp/fastrpc_shell_unsigned_3":
                    dspdir + "cdsp/fastrpc_shell_unsigned_3"}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], symlinks=symlinks), 0)
            cdidir = Path(d) / "run" / "cdi"
            cdsp = json.loads(
                (cdidir / "qualcomm-fastrpc-cdsp.json").read_text())
            bypath = {m["containerPath"]: m
                      for m in cdsp["containerEdits"]["mounts"]}
            self.assertIn("/usr/share/hexagon-dsp/cdsp/fastrpc_shell_unsigned_3", bypath)
            self.assertIn("/usr/share/hexagon-dsp/cdsp/libc++.so.1", bypath)
            # The host firmware directory itself is not mounted, under its own
            # path or any other: only the files inside it are.
            self.assertFalse([path for path in bypath
                              if path.startswith(dspdir.rstrip("/"))], bypath)
            gpu = json.loads((cdidir / "qualcomm-gpu.json").read_text())
            self.assertNotIn("mounts", gpu["containerEdits"])

    def test_fastrpc_adsp_spec_carries_its_own_file_mounts(self):
        # End to end: the adsp spec is given the adsp*/ files and no cdsp*/
        # path at all, the firmware being per DSP, so the compute DSP's
        # firmware is not in a spec that cannot load it.
        dspdir = self._dspdir("cdsp/fastrpc_shell_unsigned_3",
                              "cdsp/libc++.so.1",
                              "adsp/fastrpc_shell_unsigned_3",
                              "adsp/libc++.so.1")
        symlinks = {"/usr/lib/dsp/adsp/fastrpc_shell_unsigned_3":
                    dspdir + "adsp/fastrpc_shell_unsigned_3"}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], symlinks=symlinks), 0)
            spec = json.loads((Path(d) / "run" / "cdi"
                               / "qualcomm-fastrpc-adsp.json").read_text())
            mounts = spec["containerEdits"]["mounts"]
            bypath = {m["containerPath"]: m for m in mounts}
            self.assertIn("/usr/share/hexagon-dsp/adsp/fastrpc_shell_unsigned_3", bypath)
            self.assertIn("/usr/share/hexagon-dsp/adsp/libc++.so.1", bypath)
            self.assertFalse([path for path in bypath
                              if path.startswith(dspdir.rstrip("/"))], bypath)
            self.assertEqual([m for m in mounts
                              if "cdsp" in m["hostPath"]
                              or "cdsp" in m["containerPath"]], [])


class DspFirmwareBoardTests(unittest.TestCase):
    """Tests for resolving a board's DSP firmware directory.

    The hexagon-dsp packaging names a firmware directory per devicetree model
    in a conf.d YAML file, which is the board's own answer and what the
    generator asks first. An image carrying no such file is answered by the
    symlinks the rpcd services leave in /usr/lib/dsp, and a host with neither
    by globbing. These cover all three.
    """

    def _present(self, files=(), conffiles=None, symlinks=None):
        """Serve a synthetic host to the generator's globs, opens and stats.

        The board directories are absolute paths that do not exist on the
        machine running the tests, so they are presented rather than created:
        what is being tested is which of them the generator looks in, and a
        real Qualcomm host's own /usr/share must not answer instead.
        """
        fileset = set(files)
        conffiles = conffiles or {}
        symlinks = symlinks or {}
        confdirs = {str(PurePosixPath(p).parent) for p in conffiles}

        def fake_glob(pattern, *args, **kwargs):
            if pattern == gen.DSP_SYMLINK_GLOB:
                # Symlinks and any real file presented in the lookup path: a
                # target that installs the firmware there has the latter.
                return sorted(set(symlinks) | {p for p in fileset
                                               if path_matches(pattern, p)})
            candidates = fileset | set(conffiles)
            return sorted(p for p in candidates if path_matches(pattern, p))

        def fake_open(path, *args, **kwargs):
            if path in conffiles:
                return io.StringIO(conffiles[path])
            raise FileNotFoundError(2, "No such file or directory", path)

        def fake_isdir(path):
            key = os.fspath(path)
            return (key in confdirs
                    or any(f.startswith(key.rstrip("/") + "/") for f in fileset))

        patchers = [
            mock.patch.object(gen.glob, "glob", fake_glob),
            mock.patch("builtins.open", fake_open),
            mock.patch.object(gen.os.path, "isdir", fake_isdir),
            mock.patch.object(gen.os.path, "realpath",
                              lambda p: symlinks.get(os.fspath(p), os.fspath(p))),
            mock.patch.object(gen.os.path, "exists",
                              lambda p: os.fspath(p) in fileset),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- the configuration file ------------------------------------------

    def test_the_library_path_is_read_from_the_configuration(self):
        # The file as the image ships it: an SPDX header, a schema annotation
        # and one machine entry, none of which a YAML library is needed for.
        self._present(conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE})
        self.assertEqual(
            gen.parse_dsp_conf(QCOM_CONF_FILE),
            {RB3GEN2_MACHINE: {"DSP_LIBRARY_PATH": RB3GEN2_LIBRARY_PATH}})

    def test_several_machines_in_one_file_are_kept_apart(self):
        # One file covers a board family, so the model is what picks the entry.
        conf = FAKE_CONF_FILE + ("  Other Board:\n"
                                 "    DSP_LIBRARY_PATH: other/board/dsp\n")
        self._present(conffiles={QCOM_CONF_FILE: conf})
        machines = gen.parse_dsp_conf(QCOM_CONF_FILE)
        self.assertEqual(machines[RB3GEN2_MACHINE]["DSP_LIBRARY_PATH"],
                         RB3GEN2_LIBRARY_PATH)
        self.assertEqual(machines["Other Board"]["DSP_LIBRARY_PATH"],
                         "other/board/dsp")

    def test_keys_outside_the_machines_mapping_are_ignored(self):
        # Another top-level section closes the mapping, so its keys are not
        # read as machines.
        conf = FAKE_CONF_FILE + "version: 2\nother:\n  Not A Board:\n"
        self._present(conffiles={QCOM_CONF_FILE: conf})
        self.assertEqual(list(gen.parse_dsp_conf(QCOM_CONF_FILE)),
                         [RB3GEN2_MACHINE])

    def test_a_quoted_machine_name_is_unquoted(self):
        conf = ("machines:\n  '%s':\n    DSP_LIBRARY_PATH: \"%s\"\n"
                % (RB3GEN2_MACHINE, RB3GEN2_LIBRARY_PATH))
        self._present(conffiles={QCOM_CONF_FILE: conf})
        self.assertEqual(
            gen.parse_dsp_conf(QCOM_CONF_FILE),
            {RB3GEN2_MACHINE: {"DSP_LIBRARY_PATH": RB3GEN2_LIBRARY_PATH}})

    def test_an_unreadable_configuration_is_no_machines(self):
        # Nothing is resolved from it and nothing raises out of the run.
        self._present()
        self.assertEqual(gen.parse_dsp_conf(QCOM_CONF_FILE), {})

    # ---- which directory that resolves to --------------------------------

    def test_the_configuration_names_the_firmware_directory(self):
        # DSP_LIBRARY_PATH is relative to the directory holding the conf.d, so
        # the qcom conf.d resolves it under /usr/share/qcom. The conf.d is
        # returned with it, being what the path resolves against and what the
        # container is given.
        self._present(files=[RB3GEN2_DSP + "/cdsp/fastrpc_shell_3"],
                      conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE})
        boardconf = gen.read_dsp_library_path(RB3GEN2_MACHINE, [QCOM_CONF_DIR])
        self.assertEqual(boardconf, (QCOM_CONF_DIR, RB3GEN2_LIBRARY_PATH))
        self.assertEqual(
            gen.dsp_firmware_board_dirs(boardconf, RB3GEN2_MACHINE),
            [RB3GEN2_DSP])

    def test_the_hexagon_dsp_conf_dir_resolves_under_its_own_root(self):
        # The same relative path under the other location names a different
        # directory, which is why the conf.d it came from is what it is
        # resolved against.
        conffile = HEXAGON_CONF_DIR + "/hexagon-dsp-binaries.yaml"
        self._present(files=["/usr/share/hexagon-dsp/" + RB3GEN2_LIBRARY_PATH
                             + "/cdsp/fastrpc_shell_3"],
                      conffiles={conffile: FAKE_CONF_FILE})
        boardconf = gen.read_dsp_library_path(RB3GEN2_MACHINE,
                                              [HEXAGON_CONF_DIR])
        self.assertEqual(boardconf, (HEXAGON_CONF_DIR, RB3GEN2_LIBRARY_PATH))
        self.assertEqual(
            gen.dsp_firmware_board_dirs(boardconf, RB3GEN2_MACHINE),
            ["/usr/share/hexagon-dsp/" + RB3GEN2_LIBRARY_PATH])

    def test_a_board_the_configuration_does_not_name(self):
        # Nothing is resolved for it, and with no symlinks either there is no
        # firmware to mount.
        self._present(conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE})
        self.assertIsNone(
            gen.read_dsp_library_path("Some Other Board", [QCOM_CONF_DIR]))
        self.assertEqual(
            gen.dsp_firmware_board_dirs(None, "Some Other Board"), [])

    def test_a_host_with_no_board_name_resolves_nothing(self):
        # The model is the key into the configuration, so without it there is
        # no entry to look up.
        self._present(conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE})
        self.assertIsNone(gen.read_dsp_library_path(None, [QCOM_CONF_DIR]))

    def test_a_library_path_that_is_not_there_is_not_used(self):
        # The configuration names a directory the image did not install: the
        # symlinks are what answer instead, and with none there is nothing to
        # mount.
        self._present(conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE})
        boardconf = gen.read_dsp_library_path(RB3GEN2_MACHINE, [QCOM_CONF_DIR])
        self.assertEqual(
            gen.dsp_firmware_board_dirs(boardconf, RB3GEN2_MACHINE), [])

    # ---- the symlink fallback --------------------------------------------

    def test_the_symlinks_resolve_the_firmware_directory(self):
        # No conf.d on this image: what the rpcd services symlinked into
        # /usr/lib/dsp leads back to the directory they picked.
        shell = SA8775P_DSP + "/cdsp/fastrpc_shell_3"
        self._present(files=[shell],
                      symlinks={"/usr/lib/dsp/cdsp/fastrpc_shell_3": shell})
        self.assertEqual(gen.resolve_dsp_firmware_dirs(), [SA8775P_DSP])
        self.assertEqual(gen.dsp_firmware_board_dirs(None, RB3GEN2_MACHINE),
                         [SA8775P_DSP])

    def test_every_symlink_resolves_to_one_directory(self):
        # Several DSPs' symlinks point into one board directory; it is listed
        # once, and it is the directory the per-DSP sub-directories sit in.
        self._present(
            files=[SA8775P_DSP + "/cdsp/fastrpc_shell_3",
                   SA8775P_DSP + "/adsp/fastrpc_shell_3"],
            symlinks={"/usr/lib/dsp/cdsp/fastrpc_shell_3":
                      SA8775P_DSP + "/cdsp/fastrpc_shell_3",
                      "/usr/lib/dsp/adsp/fastrpc_shell_3":
                      SA8775P_DSP + "/adsp/fastrpc_shell_3"})
        self.assertEqual(gen.resolve_dsp_firmware_dirs(), [SA8775P_DSP])

    def test_a_dangling_symlink_resolves_nothing(self):
        # A leftover link into a directory the image no longer carries must
        # not become a firmware directory the spec points at.
        self._present(symlinks={"/usr/lib/dsp/cdsp/fastrpc_shell_3":
                                SA8775P_DSP + "/cdsp/fastrpc_shell_3"})
        self.assertEqual(gen.resolve_dsp_firmware_dirs(), [])

    def test_real_files_in_the_lookup_path_resolve_to_it(self):
        # A target that installs the firmware there rather than symlinking it
        # resolves to the files themselves, which answers the same question.
        self._present(files=["/usr/lib/dsp/cdsp/fastrpc_shell_3"])
        self.assertEqual(gen.resolve_dsp_firmware_dirs(), ["/usr/lib/dsp"])

    def test_the_configuration_is_preferred_over_the_symlinks(self):
        # Both are there: the board's own answer wins over what a service
        # happened to leave behind.
        self._present(
            files=[RB3GEN2_DSP + "/cdsp/fastrpc_shell_3",
                   SA8775P_DSP + "/cdsp/fastrpc_shell_3"],
            conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE},
            symlinks={"/usr/lib/dsp/cdsp/fastrpc_shell_3":
                      SA8775P_DSP + "/cdsp/fastrpc_shell_3"})
        boardconf = gen.read_dsp_library_path(RB3GEN2_MACHINE, [QCOM_CONF_DIR])
        self.assertEqual(
            gen.dsp_firmware_board_dirs(boardconf, RB3GEN2_MACHINE),
            [RB3GEN2_DSP])

    # ---- the conf.d mount -------------------------------------------------

    def test_the_configuration_is_mounted_at_one_container_path(self):
        # Whichever of the two locations the host keeps it in, the container
        # reads the configuration at /usr/share/hexagon-dsp/conf.d, as it
        # reads the firmware under /usr/share/hexagon-dsp: nothing in there
        # has to know which layout the host used.
        for confdir in (QCOM_CONF_DIR, HEXAGON_CONF_DIR):
            self.assertEqual(gen.generate_dsp_conf_cdi(confdir), [{
                "hostPath": confdir,
                "containerPath": HEXAGON_CONF_DIR,
                "options": ["nosuid", "ro", "bind"],
            }], confdir)

    def test_no_configuration_directory_mounts_nothing(self):
        self.assertEqual(gen.generate_dsp_conf_cdi(None), [])

    def test_the_conf_dir_that_named_the_board_is_the_one_mounted(self):
        # Both locations installed: the container has to be given the file its
        # own board is in, or DSP_LIBRARY_PATH resolves to nothing in there.
        # Only one can be handed over, both arriving at a single path.
        boardconf = (HEXAGON_CONF_DIR, RB3GEN2_LIBRARY_PATH)
        self.assertEqual(
            gen.dsp_conf_mount_dir(boardconf,
                                   [QCOM_CONF_DIR, HEXAGON_CONF_DIR]),
            HEXAGON_CONF_DIR)

    def test_an_unnamed_board_mounts_the_first_conf_dir(self):
        # Nothing named this board, so there is nothing to choose between the
        # two; the container is no worse off reading what the host reads.
        self.assertEqual(
            gen.dsp_conf_mount_dir(None, [QCOM_CONF_DIR, HEXAGON_CONF_DIR]),
            QCOM_CONF_DIR)
        self.assertIsNone(gen.dsp_conf_mount_dir(None, []))

    def test_both_configuration_directories_are_looked_for(self):
        self.assertEqual(gen.DSP_CONF_DIRS,
                         (QCOM_CONF_DIR, HEXAGON_CONF_DIR))

    # ---- what that means per class ---------------------------------------

    def test_only_the_resolved_directory_is_looked_in(self):
        # The whole point: a resolved board uses its own directory and the
        # other boards an image carries are never looked at, so the spec
        # cannot carry firmware for hardware the host is not running on.
        self._present(files=[SA8775P_DSP + "/cdsp/fastrpc_shell_3",
                             RB3GEN2_DSP + "/cdsp/fastrpc_shell_3"])
        self.assertEqual(
            gen.find_dsp_firmware_dirs("fastrpc-cdsp", [SA8775P_DSP]),
            [SA8775P_DSP])

    def test_a_resolved_board_never_takes_another_boards_firmware(self):
        # The board resolved and its directory holds nothing for this class,
        # which is a DSP the image installed no firmware for. Nothing else is
        # looked in: firmware built for another SoC is worse than none.
        self._present(files=[SA8775P_DSP + "/adsp/fastrpc_shell_3",
                             RB3GEN2_DSP + "/cdsp/fastrpc_shell_3"])
        self.assertEqual(
            gen.find_dsp_firmware_dirs("fastrpc-cdsp", [SA8775P_DSP]), [])

    def test_no_firmware_directory_is_mounted(self):
        # The resolved board directory decides where the files come from and
        # is not itself handed over: a spec mounts the files the FastRPC client
        # opens and no directory, so no path of another DSP's firmware comes
        # along inside one.
        self._present(files=[SA8775P_DSP + "/cdsp/fastrpc_shell_3",
                             SA8775P_DSP + "/cdsp1/fastrpc_shell_4",
                             SA8775P_DSP + "/adsp/fastrpc_shell_3"])
        mounts = gen.generate_dsp_files_cdi("fastrpc-cdsp", [SA8775P_DSP])
        self.assertEqual(
            sorted(m["containerPath"] for m in mounts),
            ["/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
             "/usr/share/hexagon-dsp/cdsp1/fastrpc_shell_4"])
        for mount in mounts:
            self.assertTrue(mount["containerPath"].startswith("/usr/share/hexagon-dsp/"),
                            mount["containerPath"])

    def test_the_resolved_board_reaches_the_specs(self):
        # End to end: the board's configuration picks the firmware directory,
        # each DSP class is given its own files out of it under the one
        # container path, and the other board directory the image carries
        # reaches no spec at all.
        boardfiles = [RB3GEN2_DSP + "/cdsp/fastrpc_shell_3",
                      RB3GEN2_DSP + "/adsp/fastrpc_shell_3",
                      RB3GEN2_DSP + "/gdsp0/fastrpc_shell_3",
                      SA8775P_DSP + "/cdsp/fastrpc_shell_3"]
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               devicetree=RB3GEN2_MACHINE,
                               conffiles={QCOM_CONF_FILE: FAKE_CONF_FILE},
                               boardfiles=boardfiles)
            self.assertEqual(rc, 0)
            cdidir = Path(d) / "run" / "cdi"
            for cdiclass, dspname in (("fastrpc-cdsp", "cdsp"),
                                      ("fastrpc-adsp", "adsp"),
                                      ("fastrpc-gdsp", "gdsp0")):
                spec = json.loads(
                    (cdidir / ("qualcomm-%s.json" % cdiclass)).read_text())
                mounts = {(m["hostPath"], m["containerPath"])
                          for m in spec["containerEdits"]["mounts"]}
                self.assertIn((RB3GEN2_DSP + "/" + dspname + "/fastrpc_shell_3",
                               CONTAINER_DSP_DIR + "/" + dspname
                               + "/fastrpc_shell_3"), mounts, cdiclass)
                # Another board's firmware, the other DSPs' of this one, and
                # the directory the files came from: none of them is mounted.
                self.assertFalse([path for path, _ in mounts
                                  if path.startswith(SA8775P_DSP)], cdiclass)
                self.assertFalse([path for path, _ in mounts
                                  if path.startswith(RB3GEN2_DSP)
                                  and not path.startswith(RB3GEN2_DSP + "/"
                                                          + dspname + "/")],
                                 cdiclass)
                # The configuration the directory was resolved from is handed
                # over too, at the one container path, and is the only
                # directory a spec mounts.
                self.assertIn((QCOM_CONF_DIR, HEXAGON_CONF_DIR), mounts,
                              cdiclass)

    def test_a_board_that_answers_with_nothing_gets_no_firmware(self):
        # No configuration and no symlinks: there is nothing to resolve, and
        # nothing is guessed at. The spec is still written for the device
        # nodes and the board name -- those do not depend on the firmware --
        # and carries no firmware mount at all.
        boardfiles = ["/usr/share/vendor/soc/vendor/board/dsp/cdsp/fastrpc_shell_3"]
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], devicetree=FAKE_MACHINE,
                               boardfiles=boardfiles)
            self.assertEqual(rc, 0)
            spec = json.loads((Path(d) / "run" / "cdi"
                               / "qualcomm-fastrpc-cdsp.json").read_text())
            mounts = spec["containerEdits"]["mounts"]
            self.assertEqual(
                [m for m in mounts
                 if m["containerPath"].startswith(CONTAINER_DSP_DIR)], [])
            # Another board's firmware is there to be found and is not taken.
            self.assertEqual([m for m in mounts
                              if m["hostPath"].startswith("/usr/share/")], [])
            # The device nodes and the board name still reach the spec.
            self.assertTrue(spec["devices"])
            self.assertEqual(spec["containerEdits"]["env"],
                             ["MACHINE_NAME=" + FAKE_MACHINE])


class SocketContainerPathTests(unittest.TestCase):
    """Tests for where a host socket is mounted inside the container."""

    def test_nothing_is_mounted_under_run_user(self):
        # The container resolves its sockets under /run, so no host layout may
        # produce a container path in a per-user runtime directory.
        for socket in ("/run/wayland-0", "/run/wayland-1",
                       "/run/user/1000/wayland-0", "/run/user/1234/wayland-0",
                       "/run/pulse/native", "/run/user/1000/pulse/native",
                       "/run/user/1001/pulse/native",
                       "/run/cam_server/le_camera_socket"):
            container = gen.socket_container_path(socket)
            self.assertFalse(container.startswith("/run/user/"),
                             "%s -> %s" % (socket, container))

    def test_wayland_per_user_socket_moves_to_run(self):
        # A session compositor's socket is lifted out of the per-user runtime
        # directory, whatever uid owns it on the host.
        self.assertEqual(gen.socket_container_path("/run/user/1000/wayland-0"),
                         "/run/wayland-0")
        self.assertEqual(gen.socket_container_path("/run/user/1234/wayland-0"),
                         "/run/wayland-0")

    def test_wayland_basename_is_preserved(self):
        # A second display keeps its own name rather than collapsing onto the
        # first one.
        self.assertEqual(gen.socket_container_path("/run/user/1000/wayland-1"),
                         "/run/wayland-1")

    def test_system_socket_is_left_alone(self):
        # Already where a client in the container looks; remapping it would
        # only rewrite the path to itself.
        for socket in ("/run/wayland-0", "/run/wayland-1",
                       "/run/pulse/native"):
            self.assertEqual(gen.socket_container_path(socket), socket)

    def test_audio_socket_is_pinned_for_every_host_layout(self):
        # The audio socket is always presented at /run/pulse/native, whether
        # the host runs it system-wide or under any uid, so a container client
        # sees one stable path. 'pulse' is in the mapped directory because the
        # socket's own basename is 'native'.
        #
        # This is the path each socket asks for on its own; two of them in one
        # spec are separated by generate_mounts_cdi().
        for host in ("/run/pulse/native", "/run/user/1000/pulse/native",
                     "/run/user/1001/pulse/native"):
            self.assertEqual(gen.socket_container_path(host),
                             "/run/pulse/native")

    def test_unrelated_sockets_are_left_alone(self):
        self.assertEqual(gen.socket_container_path("/run/cam_server/le_camera_socket"),
                         "/run/cam_server/le_camera_socket")

    def test_remap_does_not_reach_into_subdirectories(self):
        # The '*' in '/run/user/*/wayland-*' is one segment each, so a path
        # below a wayland-named directory is not treated as the socket itself.
        self.assertEqual(gen.socket_container_path("/run/user/1000/wayland-dir/socket"),
                         "/run/user/1000/wayland-dir/socket")

    def test_mounts_carry_the_remapped_container_path(self):
        mounts = gen.generate_mounts_cdi(["/run/wayland-0",
                                          "/run/user/1001/wayland-1",
                                          "/run/user/1001/pulse/native",
                                          "/run/cam_server/le_camera_socket"])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [("/run/wayland-0", "/run/wayland-0"),
                          ("/run/user/1001/wayland-1", "/run/wayland-1"),
                          ("/run/user/1001/pulse/native", "/run/pulse/native"),
                          ("/run/cam_server/le_camera_socket",
                           "/run/cam_server/le_camera_socket")])

    def test_colliding_sockets_are_mounted_side_by_side(self):
        # Two sessions each serve a 'native' socket and the remapping drops the
        # uid that told them apart, so both ask for /run/pulse/native. Neither
        # is dropped and neither overrides the other: the first keeps the path
        # a stock image connects to with no configuration and the second is
        # suffixed.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/pulse/native",
                                          "/run/user/1001/pulse/native",
                                          "/run/user/1002/pulse/native"])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [("/run/user/1000/pulse/native", "/run/pulse/native"),
                          ("/run/user/1001/pulse/native", "/run/pulse/native-1"),
                          ("/run/user/1002/pulse/native", "/run/pulse/native-2")])

    def test_a_lone_socket_is_never_suffixed(self):
        # The single-session host, which is every ordinary target: nothing
        # collides, so the path is the one the client already resolves.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/pulse/native"])
        self.assertEqual(mounts[0]["containerPath"], "/run/pulse/native")

    def test_every_colliding_socket_keeps_its_own_host_path(self):
        # The suffix separates the container paths only; each mount still
        # names the host socket it came from, so no session's socket is lost.
        sockets = ["/run/user/1000/pulse/native", "/run/user/1001/pulse/native"]
        mounts = gen.generate_mounts_cdi(sockets)
        self.assertEqual([m["hostPath"] for m in mounts], sockets)
        self.assertEqual(len({m["containerPath"] for m in mounts}), len(sockets))

    def test_one_session_displays_do_not_collide(self):
        # One compositor numbers its own sockets, so the remapping that drops
        # the uid still leaves distinct paths: a session's own displays are
        # never suffixed and keep the numbers the host gave them.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/wayland-0",
                                          "/run/user/1000/wayland-1"])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [("/run/user/1000/wayland-0", "/run/wayland-0"),
                          ("/run/user/1000/wayland-1", "/run/wayland-1")])

    def test_a_second_session_display_is_suffixed(self):
        # Two logged-in users each have a compositor numbering from zero, so
        # both ask for /run/wayland-0. The later one is suffixed rather than
        # shadowing the first, so both displays stay reachable.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/wayland-0",
                                          "/run/user/1001/wayland-0"])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [("/run/user/1000/wayland-0", "/run/wayland-0"),
                          ("/run/user/1001/wayland-0", "/run/wayland-0-1")])

    def test_every_session_display_is_mounted_once(self):
        # Two users with two displays each: every socket keeps its own mount
        # point, so none of the four is lost or overridden.
        sockets = ["/run/user/1000/wayland-0", "/run/user/1000/wayland-1",
                   "/run/user/1001/wayland-0", "/run/user/1001/wayland-1"]
        mounts = gen.generate_mounts_cdi(sockets)
        self.assertEqual([m["hostPath"] for m in mounts], sockets)
        self.assertEqual([m["containerPath"] for m in mounts],
                         ["/run/wayland-0", "/run/wayland-1",
                          "/run/wayland-0-1", "/run/wayland-1-1"])

    def test_the_suffix_is_not_a_session_number(self):
        # The counter runs per container path, not per session, so a session
        # whose second display collides with nothing keeps the bare name while
        # its first display is suffixed. hostPath is what says whose socket a
        # mount is; the suffix only keeps the mount points distinct.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/wayland-0",
                                          "/run/user/1001/wayland-0",
                                          "/run/user/1001/wayland-1"])
        self.assertEqual([(m["hostPath"], m["containerPath"]) for m in mounts],
                         [("/run/user/1000/wayland-0", "/run/wayland-0"),
                          ("/run/user/1001/wayland-0", "/run/wayland-0-1"),
                          ("/run/user/1001/wayland-1", "/run/wayland-1")])

    def test_both_classes_are_suffixed_in_one_run(self):
        # Both classes on a host with two sessions: each pair asks for one
        # path, and each is separated in the directory its own client looks in.
        mounts = gen.generate_mounts_cdi(["/run/user/1000/wayland-0",
                                          "/run/user/1001/wayland-0",
                                          "/run/user/1000/pulse/native",
                                          "/run/user/1001/pulse/native"])
        self.assertEqual([m["containerPath"] for m in mounts],
                         ["/run/wayland-0",
                          "/run/wayland-0-1",
                          "/run/pulse/native",
                          "/run/pulse/native-1"])

    def test_which_sockets_are_suffixed_on_collision(self):
        # The audio and display sockets are told apart by nothing but the uid
        # the remapping drops, so both are suffixed when they collide; the
        # camera socket keeps the path it was given.
        for socket in ("/run/pulse/native", "/run/user/1000/pulse/native",
                       "/run/wayland-0", "/run/user/1000/wayland-0"):
            self.assertTrue(gen.socket_suffixed_on_collision(socket), socket)
        self.assertFalse(
            gen.socket_suffixed_on_collision("/run/cam_server/le_camera_socket"))

    def test_a_shadowed_mount_is_logged(self):
        # The last-resort guard for a socket that is not suffixed, the camera
        # server's being the only one left. No CLASS_SOCKETS layout reaches it
        # -- one host path is globbed once -- but a mount that is invisible in
        # the container must say so rather than be left to be guessed at.
        camerasocket = "/run/cam_server/le_camera_socket"
        with self.assertLogs(level="WARNING") as logs:
            gen.generate_mounts_cdi([camerasocket, camerasocket])
        self.assertIn(camerasocket, logs.output[0])

    def test_suffix_skips_a_container_path_already_taken(self):
        # A host serving 'native-1' itself must not have a generated suffix
        # land on top of it; the counter walks past whatever is taken.
        self.assertEqual(
            gen.unique_container_path("/run/pulse/native",
                                      {"/run/pulse/native",
                                       "/run/pulse/native-1"}),
            "/run/pulse/native-2")

    def test_suffix_names_the_mount_point_not_its_directory(self):
        # Suffixing 'pulse' instead would put the socket in a directory no
        # client looks in; it is the mount point's own name that must differ.
        self.assertEqual(
            gen.unique_container_path("/run/pulse/native", {"/run/pulse/native"}),
            "/run/pulse/native-1")

    def test_glob_matches_segments_not_separators(self):
        self.assertTrue(gen.glob_matches("/run/wayland-*", "/run/wayland-0"))
        self.assertFalse(gen.glob_matches("/run/wayland-*", "/run/wayland-0/x"))
        self.assertTrue(gen.glob_matches("/run/user/*/pulse/native",
                                         "/run/user/1000/pulse/native"))
        self.assertFalse(gen.glob_matches("/run/user/*/pulse/native",
                                          "/run/user/1000/sub/pulse/native"))


class SocketLookupTests(unittest.TestCase):
    """Tests for find_sockets()' pattern ordering and fallback."""

    def _find(self, cdiclass, present):
        """Run find_sockets() against a synthetic set of existing paths."""
        presentset = set(present)

        def fake_glob(pattern, *args, **kwargs):
            return sorted(path for path in presentset
                          if path_matches(pattern, path))

        with mock.patch.object(gen.glob, "glob", fake_glob):
            return gen.find_sockets(cdiclass)

    def test_first_pattern_wins(self):
        # /run/pulse/native is listed before the per-user runtime socket, so a
        # system-wide sound server is what gets mounted.
        self.assertEqual(
            self._find("audio", ["/run/pulse/native",
                                 "/run/user/1000/pulse/native"]),
            ["/run/pulse/native"])

    def test_falls_back_to_later_pattern(self):
        self.assertEqual(
            self._find("audio", ["/run/user/1000/pulse/native"]),
            ["/run/user/1000/pulse/native"])

    def test_wayland_fallback_under_any_uid(self):
        self.assertEqual(self._find("display", ["/run/user/1234/wayland-0"]),
                         ["/run/user/1234/wayland-0"])

    def test_every_match_of_the_winning_pattern_is_kept(self):
        # One pattern can name several sockets; a compositor exposing two
        # displays hands both to the container.
        self.assertEqual(
            self._find("display", ["/run/wayland-0", "/run/wayland-1"]),
            ["/run/wayland-0", "/run/wayland-1"])

    def test_matches_of_a_losing_pattern_are_dropped(self):
        # Only the winning pattern contributes, so the same compositor is not
        # mounted twice under two different paths.
        self.assertEqual(
            self._find("display", ["/run/wayland-0",
                                   "/run/user/1000/wayland-0"]),
            ["/run/wayland-0"])

    def test_no_match_yields_empty(self):
        self.assertEqual(self._find("display", []), [])
        self.assertEqual(self._find("audio", ["/run/user/1000/wayland-0"]), [])

    def test_compositor_lock_files_are_not_sockets(self):
        # A compositor keeps a '<socket>.lock' next to each socket it serves,
        # and the wayland glob matches it too. Only the socket is mounted: the
        # lock is a file no client connects to and the host's to hold.
        self.assertEqual(
            self._find("display", ["/run/user/1000/wayland-1",
                                   "/run/user/1000/wayland-1.lock"]),
            ["/run/user/1000/wayland-1"])
        self.assertEqual(
            self._find("display", ["/run/wayland-0", "/run/wayland-0.lock",
                                   "/run/wayland-1", "/run/wayland-1.lock"]),
            ["/run/wayland-0", "/run/wayland-1"])

    def test_a_pattern_matching_only_a_lock_file_falls_through(self):
        # A runtime directory left with a stale lock and no socket is not a
        # running compositor, so the next pattern still gets its turn.
        self.assertEqual(
            self._find("display", ["/run/wayland-0.lock",
                                   "/run/user/1000/wayland-0"]),
            ["/run/user/1000/wayland-0"])
        self.assertEqual(self._find("display", ["/run/wayland-0.lock"]), [])

    def test_wildcard_does_not_span_directories(self):
        # '/run/user/*/pulse/native' must not match a deeper path; a '*' is one
        # path segment, as in glob.
        self.assertEqual(
            self._find("audio", ["/run/user/1000/sub/pulse/native"]), [])

    def test_unknown_class_has_no_sockets(self):
        self.assertEqual(self._find("gpu", ["/run/pulse/native"]), [])

    def test_a_greeter_display_is_dropped_for_a_login(self):
        # A display manager's compositor is not what a container is meant to
        # draw on, so the login's session is what the class carries.
        self.assertEqual(
            self._find("display", ["/run/user/123/wayland-0",
                                   "/run/user/1000/wayland-0"]),
            ["/run/user/1000/wayland-0"])

    def test_every_login_keeps_every_display(self):
        # The host of the two-user case: both sessions are passed through with
        # all their displays, and only the greeter is left out. The collision
        # the two wayland-0 sockets make is settled at mount time, not here.
        self.assertEqual(
            self._find("display", ["/run/user/123/wayland-0",
                                   "/run/user/1000/wayland-0",
                                   "/run/user/1000/wayland-1",
                                   "/run/user/1001/wayland-0",
                                   "/run/user/1001/wayland-1"]),
            ["/run/user/1000/wayland-0", "/run/user/1000/wayland-1",
             "/run/user/1001/wayland-0", "/run/user/1001/wayland-1"])

    def test_a_system_session_display_is_still_passed_through(self):
        # Nothing but a display manager on the host: its compositor is the
        # only display there is, so it is mounted rather than the class being
        # skipped for want of a regular login. Two of them are both kept,
        # since there is no login to prefer and the mount points are separated
        # anyway.
        self.assertEqual(
            self._find("display", ["/run/user/123/wayland-0"]),
            ["/run/user/123/wayland-0"])
        self.assertEqual(
            self._find("display", ["/run/user/123/wayland-0",
                                   "/run/user/125/wayland-0"]),
            ["/run/user/123/wayland-0", "/run/user/125/wayland-0"])

    def test_two_logins_are_ordered_the_same_every_boot(self):
        # Which session keeps the unsuffixed /run/wayland-0 must not depend on
        # which one came up first, so the sockets are returned lowest uid
        # first whatever order the host presented them in.
        for order in (["/run/user/1000/wayland-0", "/run/user/1001/wayland-0"],
                      ["/run/user/1001/wayland-0", "/run/user/1000/wayland-0"]):
            self.assertEqual(self._find("display", order),
                             ["/run/user/1000/wayland-0",
                              "/run/user/1001/wayland-0"])

    def test_a_system_wide_compositor_needs_no_filtering(self):
        # The first pattern already stops at /run, and sockets served there
        # belong to no session, so the uid filter has nothing to drop.
        self.assertEqual(
            self._find("display", ["/run/wayland-0", "/run/wayland-1"]),
            ["/run/wayland-0", "/run/wayland-1"])

    def test_a_session_left_with_only_a_lock_file_is_not_a_login(self):
        # A stale lock is not a running compositor. The lock is dropped before
        # the sessions are filtered, so a user runtime directory holding
        # nothing else does not count as the login that would have had the
        # greeter's display dropped.
        self.assertEqual(
            self._find("display", ["/run/user/123/wayland-0",
                                   "/run/user/1000/wayland-0.lock"]),
            ["/run/user/123/wayland-0"])

    def test_the_sessions_not_passed_through_are_logged(self):
        # The sockets exist and were deliberately left out, so a target whose
        # uids are not what this expects can be diagnosed from the run.
        with self.assertLogs(level="INFO") as logs:
            self._find("display", ["/run/user/123/wayland-0",
                                   "/run/user/1000/wayland-0"])
        skipped = [line for line in logs.output if "Skipping" in line]
        self.assertEqual(len(skipped), 1)
        self.assertIn("/run/user/123", skipped[0])
        self.assertIn(str(gen.SESSION_UID_MIN), skipped[0])

    def test_a_greeter_audio_socket_is_dropped_for_a_login(self):
        # The audio class is filtered by uid as the display class is: a
        # greeter's sound server is not what a container is meant to play
        # through, and it would take the unsuffixed name if it sorted first.
        self.assertEqual(
            self._find("audio", ["/run/user/123/pulse/native",
                                 "/run/user/1000/pulse/native"]),
            ["/run/user/1000/pulse/native"])

    def test_every_login_keeps_its_audio_socket(self):
        # Both logins are passed through, lowest uid first, so which one keeps
        # /run/pulse/native does not depend on the order found.
        self.assertEqual(
            self._find("audio", ["/run/user/1001/pulse/native",
                                 "/run/user/1000/pulse/native",
                                 "/run/user/123/pulse/native"]),
            ["/run/user/1000/pulse/native", "/run/user/1001/pulse/native"])

    def test_a_system_session_audio_socket_is_still_passed_through(self):
        # A host with nothing logged in: that sound server is the only one
        # there is, so it is mounted rather than the class being skipped.
        self.assertEqual(
            self._find("audio", ["/run/user/123/pulse/native"]),
            ["/run/user/123/pulse/native"])


class SocketSessionTests(unittest.TestCase):
    """Tests for which host sessions' sockets a class is kept to."""

    def test_runtime_uid_is_read_from_the_path(self):
        self.assertEqual(gen.socket_runtime_uid("/run/user/1000/wayland-0"),
                         1000)
        self.assertEqual(gen.socket_runtime_uid("/run/user/123/pulse/native"),
                         123)

    def test_a_system_wide_socket_has_no_runtime_uid(self):
        # Not in a per-user runtime directory, so it belongs to no session.
        for socket in ("/run/wayland-0", "/run/pulse/native",
                       "/run/cam_server/le_camera_socket"):
            self.assertIsNone(gen.socket_runtime_uid(socket), socket)

    def test_an_unnumbered_runtime_directory_has_no_uid(self):
        # /run/user holds the numbered directories logind creates; anything
        # else there is not a session this can rank.
        self.assertIsNone(gen.socket_runtime_uid("/run/user/shared/wayland-0"))

    def test_a_single_session_is_returned_untouched(self):
        # Every ordinary target, which must not pay for the multi-session case.
        sockets = ["/run/user/1000/wayland-0", "/run/user/1000/wayland-1"]
        self.assertEqual(gen.select_login_sockets("display", sockets), sockets)

    def test_a_system_account_session_is_dropped(self):
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/user/123/wayland-0",
                                      "/run/user/1000/wayland-0"]),
            ["/run/user/1000/wayland-0"])

    def test_every_login_session_is_kept(self):
        # The point of the filter: it drops system accounts, not sessions it
        # has already seen one of.
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/user/123/wayland-0",
                                      "/run/user/1000/wayland-0",
                                      "/run/user/1001/wayland-0"]),
            ["/run/user/1000/wayland-0", "/run/user/1001/wayland-0"])

    def test_a_system_wide_socket_belongs_to_no_session(self):
        # It is the path a client in the container is pointed at and is not a
        # session's, so the uid floor has no hold on it. find_sockets() never
        # produces it next to a per-user one, stopping at the first pattern.
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/wayland-0",
                                      "/run/user/1000/wayland-0"]),
            ["/run/wayland-0", "/run/user/1000/wayland-0"])

    def test_the_order_does_not_depend_on_the_order_found(self):
        sockets = ["/run/user/1000/wayland-0", "/run/user/123/wayland-0",
                   "/run/user/1001/wayland-0"]
        kept = ["/run/user/1000/wayland-0", "/run/user/1001/wayland-0"]
        self.assertEqual(gen.select_login_sockets("display", sockets), kept)
        self.assertEqual(gen.select_login_sockets("display",
                                                  list(reversed(sockets))),
                         kept)

    def test_the_sessions_are_ordered_by_uid_not_by_path(self):
        # The uid decides which session keeps the unsuffixed container path,
        # and the paths sort as strings: '/run/user/10001' comes before
        # '/run/user/1002' that way, and uid 1002 is the lower session.
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/user/10001/wayland-0",
                                      "/run/user/1002/wayland-0"]),
            ["/run/user/1002/wayland-0", "/run/user/10001/wayland-0"])

    def test_one_sessions_displays_keep_the_compositors_order(self):
        # The uid is the first key and the path the tie-break, so a session's
        # own displays stay in the order their compositor numbered them.
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/user/1000/wayland-1",
                                      "/run/user/1000/wayland-0"]),
            ["/run/user/1000/wayland-0", "/run/user/1000/wayland-1"])

    def test_the_uid_floor_is_ranked_and_not_pinned(self):
        # A target whose users run as something other than 1000 is handled the
        # same way, so nothing here may depend on that uid existing.
        self.assertEqual(
            gen.select_login_sockets("display",
                                     ["/run/user/123/wayland-0",
                                      "/run/user/5000/wayland-0"]),
            ["/run/user/5000/wayland-0"])

    def test_system_sessions_are_kept_when_there_is_no_login(self):
        # A host with nothing logged in: the greeter's compositor is the only
        # display there is, so dropping it would leave the class with no
        # socket at all and no display passed through.
        sockets = ["/run/user/123/wayland-0", "/run/user/125/wayland-0"]
        self.assertEqual(gen.select_login_sockets("display", sockets), sockets)


class BoardNameTests(unittest.TestCase):
    """Tests for the board name read off the host devicetree model node."""

    def _read(self, contents):
        """Run read_device_model() against one devicetree node.

        contents is what the node holds, or the OSError opening it raises on a
        host that has no node or will not let this one be read.
        """
        def fake_open(path, *args, **kwargs):
            self.assertEqual(path, gen.DEVICE_MODEL_HOST_PATH)
            if isinstance(contents, OSError):
                raise contents
            return io.StringIO(contents)

        with mock.patch("builtins.open", fake_open):
            return gen.read_device_model()

    def test_the_nul_terminator_is_stripped(self):
        # The node is NUL-terminated, and an environment variable cannot carry
        # a NUL at all.
        self.assertEqual(self._read(FAKE_MACHINE + "\u0000"), FAKE_MACHINE)

    def test_the_board_name_keeps_its_spaces_and_punctuation(self):
        # Only the terminator and the whitespace around the name go. A real
        # board name is several words with a comma in it, and the whole string
        # is what a client resolves its firmware paths against.
        board = "Qualcomm Technologies, Inc. Robotics RB3gen2"
        self.assertEqual(self._read(board + "\u0000\n"), board)

    def test_a_host_without_the_node_has_no_board_name(self):
        # Nothing to pass on, so no spec sets the variable.
        self.assertIsNone(
            self._read(FileNotFoundError(2, "No such file or directory")))

    def test_an_unreadable_node_has_no_board_name(self):
        # A node that is there but cannot be read is not fatal either.
        self.assertIsNone(self._read(PermissionError(13, "Permission denied")))

    def test_an_empty_node_has_no_board_name(self):
        # A node holding nothing but its terminator yields no name rather than
        # an empty MACHINE_NAME for a client to resolve paths against.
        for contents in ("", "\u0000", "  \u0000\n"):
            self.assertIsNone(self._read(contents), repr(contents))

    def test_the_node_is_mounted_even_when_it_cannot_be_read(self):
        # The mount names the node rather than the string it holds, so a
        # container reads the board name for itself whatever the generator
        # managed to read of it. The two are decided separately for this case.
        with mock.patch.object(gen.glob, "glob",
                               lambda pattern: [gen.DEVICE_MODEL_HOST_PATH]):
            mounts = gen.generate_device_model_cdi()
        self.assertEqual([m["containerPath"] for m in mounts],
                         [gen.DEVICE_MODEL_CONTAINER_PATH])


class NewClassTests(unittest.TestCase):
    """End-to-end tests for the gpu/camera/audio/display class generation."""

    def _spec(self, destdir, cdiclass):
        path = Path(destdir) / "run" / "cdi" / ("qualcomm-%s.json" % cdiclass)
        return json.loads(path.read_text())

    def _written(self, destdir):
        return sorted(p.name for p in (Path(destdir) / "run" / "cdi").glob("*.json"))

    def _devices(self, spec):
        return {d["name"]: d for d in spec["devices"]}

    def test_only_the_fastrpc_specs_set_the_board_name(self):
        # End to end, on a board exposing every class: the two fastrpc files
        # set MACHINE_NAME to the name the host devicetree holds, and no other
        # file carries an env field at all.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                              devicetree=FAKE_MACHINE), 0)
            written = sorted((Path(d) / "run" / "cdi").glob("*.json"))
            self.assertTrue(written)
            for path in written:
                spec = json.loads(path.read_text())
                if path.stem.endswith(("fastrpc-cdsp", "fastrpc-adsp",
                                       "fastrpc-gdsp")):
                    self.assertEqual(spec["containerEdits"]["env"],
                                     ["MACHINE_NAME=" + FAKE_MACHINE],
                                     path.name)
                else:
                    self.assertNotIn("env", spec["containerEdits"], path.name)
                    self.assertNotIn("MACHINE_NAME", path.read_text(),
                                     path.name)
                # Spec level, where the mounts and the groups are: a device
                # entry carries the nodes or the socket and nothing else.
                for device in spec["devices"]:
                    self.assertNotIn("env", device["containerEdits"],
                                     path.name)

    def test_the_board_name_is_the_only_variable_written(self):
        # MACHINE_NAME aside, no spec names a variable: the socket classes
        # mount a path and leave the pointing at it to the command line, so the
        # Wayland and PulseAudio pair the display and audio specs could carry
        # stay out of the generated files.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                              devicetree=FAKE_MACHINE), 0)
            for path in sorted((Path(d) / "run" / "cdi").glob("*.json")):
                spec = json.loads(path.read_text())
                self.assertLessEqual(
                    {entry.partition("=")[0]
                     for entry in spec["containerEdits"].get("env", [])},
                    {"MACHINE_NAME"}, path.name)
                for name in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY",
                             "PULSE_SERVER"):
                    self.assertNotIn(name, path.read_text(), path.name)

    def test_only_the_fastrpc_specs_mount_the_board_name(self):
        # The model node is handed to the two fastrpc classes, whose FastRPC
        # client resolves its firmware paths against the board name. Nothing in
        # the remaining classes reads one, so no other spec carries the mount.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                              devicetree=FAKE_MACHINE), 0)
            for cdiclass in ("fastrpc-cdsp", "fastrpc-adsp", "fastrpc-gdsp"):
                self.assertIn(
                    {"hostPath": DEVICETREE_MODEL,
                     "containerPath": DEVICE_MODEL_MOUNTPOINT,
                     "options": ["rbind", "ro"]},
                    self._spec(d, cdiclass)["containerEdits"]["mounts"],
                    cdiclass)
            for cdiclass in ("gpu", "v4l2", "camera", "audio", "display",
                             "dmaheap"):
                self.assertNotIn(DEVICE_MODEL_MOUNTPOINT,
                                 json.dumps(self._spec(d, cdiclass)), cdiclass)

    def test_the_board_name_mount_is_read_only(self):
        # A recursive bind, so it resolves the sysfs node rather than the mount
        # point it sits under, and read-only: nothing in the container has any
        # business writing a board name back to the host.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], devicetree=FAKE_MACHINE), 0)
            mount, = [m for m in self._spec(d, "fastrpc-cdsp")["containerEdits"]["mounts"]
                      if m["containerPath"] == DEVICE_MODEL_MOUNTPOINT]
            self.assertEqual(mount["options"], ["rbind", "ro"])
            self.assertNotIn("rw", mount["options"])

    def test_a_host_without_a_devicetree_model_mounts_nothing(self):
        # A host with no model node gets no mount rather than one naming a path
        # that is not there -- and the fastrpc specs are still written, since
        # the DSP nodes and their firmware do not depend on the board name.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], devicetree=None), 0)
            for cdiclass in ("fastrpc-cdsp", "fastrpc-adsp", "fastrpc-gdsp"):
                spec = self._spec(d, cdiclass)
                self.assertNotIn(DEVICE_MODEL_MOUNTPOINT, json.dumps(spec),
                                 cdiclass)
                self.assertNotIn(DEVICETREE_MODEL, json.dumps(spec), cdiclass)
                self.assertTrue(spec["devices"], cdiclass)

    def test_a_host_without_a_devicetree_model_sets_no_variable(self):
        # No model node, so no name to export: the fastrpc specs are still
        # written and simply set nothing, rather than an empty MACHINE_NAME a
        # client would resolve its firmware paths against.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], devicetree=None), 0)
            written = sorted((Path(d) / "run" / "cdi").glob("*.json"))
            self.assertTrue(written)
            for path in written:
                spec = json.loads(path.read_text())
                self.assertNotIn("env", spec["containerEdits"], path.name)
                self.assertNotIn("MACHINE_NAME", path.read_text(), path.name)

    def test_the_board_name_is_set_as_the_host_holds_it(self):
        # The value is the string the devicetree holds, NUL terminator
        # stripped, so a client reading MACHINE_NAME back gets exactly the name
        # its firmware paths are built from -- spaces, comma and all.
        board = "Qualcomm Technologies, Inc. Robotics RB3gen2"
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], devicetree=board), 0)
            for cdiclass in ("fastrpc-cdsp", "fastrpc-adsp", "fastrpc-gdsp"):
                self.assertEqual(
                    self._spec(d, cdiclass)["containerEdits"]["env"],
                    ["MACHINE_NAME=" + board], cdiclass)

    def test_the_board_name_reaches_the_mount_as_a_path_only(self):
        # The variable carries the string; the mount names the node and never
        # the board it holds, so what a container reads through it is whatever
        # the board it is running on says, not what this run happened to read.
        board = "Qualcomm Technologies, Inc. Robotics RB3gen2"
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], devicetree=board), 0)
            for cdiclass in ("fastrpc-cdsp", "fastrpc-adsp", "fastrpc-gdsp"):
                for mount in self._spec(d, cdiclass)["containerEdits"]["mounts"]:
                    self.assertNotIn(board, json.dumps(mount), cdiclass)

    def test_no_lock_file_reaches_the_display_spec(self):
        # End to end: the host serves wayland-1 with its lock file alongside,
        # and only the socket is handed to the container.
        sockets = ["/run/user/1000/wayland-1", "/run/user/1000/wayland-1.lock"]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], sockets=sockets), 0)
            spec = self._spec(d, "display")
            mounts = {(m["hostPath"], m["containerPath"])
                      for device in spec["devices"]
                      for m in device["containerEdits"]["mounts"]}
            self.assertEqual(sorted(mounts),
                             [("/run/user/1000/wayland-1", "/run/wayland-1")])
            self.assertNotIn(".lock", json.dumps(spec))

    def test_the_model_node_is_mounted_once_per_spec(self):
        # One mount, and at the /run path: a second entry for the same node, or
        # one left at its own /sys path, would be a spec the runtime cannot
        # apply cleanly.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                              devicetree=FAKE_MACHINE), 0)
            for path in sorted((Path(d) / "run" / "cdi").glob("*.json")):
                spec = json.loads(path.read_text())
                mounts = spec["containerEdits"].get("mounts", [])
                modelmounts = [m for m in mounts
                               if m["hostPath"] == DEVICETREE_MODEL]
                self.assertLessEqual(len(modelmounts), 1, path.name)
                for mount in modelmounts:
                    self.assertEqual(mount["containerPath"],
                                     DEVICE_MODEL_MOUNTPOINT, path.name)

    def test_gpu_carries_render_card_and_kgsl(self):
        # The GPU class exposes the render node, the DRM card node and the
        # KGSL node, each under its own device name, plus catch-alls.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d, "-C", "gpu"]), 0)
            devices = self._devices(self._spec(d, "gpu"))
            self.assertIn("renderD128", devices)
            self.assertIn("card0", devices)
            self.assertIn("kgsl-3d0", devices)
            # The render nodes have a catch-all of their own, named after
            # the node class rather than the renderD prefix, and gpu:all hands
            # over the whole stack.
            self.assertIn("render:all", devices)
            self.assertNotIn("renderD:all", devices)
            self.assertIn("gpu:all", devices)
            allpaths = sorted(n["path"] for n in
                              devices["gpu:all"]["containerEdits"]["deviceNodes"])
            self.assertEqual(allpaths, ["/dev/dri/card0", "/dev/dri/renderD128",
                                        "/dev/kgsl-3d0"])

    def test_dmaheap_names_both_spellings_of_the_system_heap(self):
        # End to end on a board exposing the heap under both names: the vendor
        # one is named after its vendor and the plain one keeps the bare class
        # name, so neither is a positional 'dmaheap-system0'.
        nodes = dict(FAKE_NODES, **{"/dev/dma_heap/*system": [
            "/dev/dma_heap/system", "/dev/dma_heap/qcom,system"]})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_generator(["-d", d], nodes), 0)
            devices = self._devices(self._spec(d, "dmaheap"))
            self.assertEqual(sorted(devices),
                             ["dmaheap-system", "dmaheap-system-qcom",
                              "dmaheap-system:all"])
            self.assertEqual(
                [n["path"] for n in
                 devices["dmaheap-system-qcom"]["containerEdits"]["deviceNodes"]],
                ["/dev/dma_heap/qcom,system"])
            self.assertEqual(
                [n["path"] for n in
                 devices["dmaheap-system"]["containerEdits"]["deviceNodes"]],
                ["/dev/dma_heap/system"])

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
                             ["render:all", "gpu:all"])

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
            paths = {n["path"] for dev in spec["devices"]
                     for n in dev["containerEdits"]["deviceNodes"]}
            self.assertEqual(paths, {"/dev/dri/renderD128", "/dev/dri/card0",
                                     "/dev/kgsl-3d0"})

    def test_camera_requires_its_socket(self):
        # No camera server socket means no camera spec, even though
        # /dev/video* nodes exist on the board.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["audio"],
                                                   FAKE_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            self.assertNotIn("qualcomm-camera.json", self._written(d))

    def test_camera_is_a_socket_mount(self):
        # The camera class passes through no device node at all: a client talks
        # to qmmf/CamX over the socket, so asking for the device is what
        # bind-mounts it.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["camera"]])
            self.assertEqual(rc, 0)
            devices = self._devices(self._spec(d, "camera"))
            self.assertIn("camera", devices)
            self.assertIn("camera:all", devices)
            edits = devices["camera"]["containerEdits"]
            self.assertNotIn("deviceNodes", edits)
            self.assertEqual([m["hostPath"] for m in edits["mounts"]],
                             [FAKE_SOCKETS["camera"]])
            self.assertEqual(edits["mounts"][0]["containerPath"],
                             FAKE_SOCKETS["camera"])
            self.assertIn("rw", edits["mounts"][0]["options"])

    def test_v4l2_still_carries_every_node(self):
        # The camera class is socket-only; v4l2 keeps every /dev/video* node.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["camera"]])
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
                self.assertEqual(edits["mounts"][0]["containerPath"],
                                 CONTAINER_SOCKETS[cdiclass])

    def test_system_wide_sockets_preferred_over_per_user(self):
        # A system-wide audio server / compositor is matched by the first
        # pattern of its class, and the per-user runtime dir is not consulted.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=[FAKE_SYSTEM_SOCKETS["audio"],
                                        FAKE_SYSTEM_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            for cdiclass in ("audio", "display"):
                edits = self._devices(self._spec(d, cdiclass))[cdiclass]["containerEdits"]
                self.assertEqual([m["hostPath"] for m in edits["mounts"]],
                                 [FAKE_SYSTEM_SOCKETS[cdiclass]])

    def test_system_wayland_socket_is_mounted_where_found(self):
        # A compositor on /run/wayland-0 is already at the path a client in the
        # container resolves, so both sides of the mount name it.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=[FAKE_SYSTEM_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            for name in ("display", "display:all"):
                edits = self._devices(self._spec(d, "display"))[name]["containerEdits"]
                self.assertEqual(edits["mounts"], [{
                    "hostPath": "/run/wayland-0",
                    "containerPath": "/run/wayland-0",
                    "options": list(gen.SOCKET_MOUNT_OPTIONS),
                }])

    def test_per_user_wayland_remap_keeps_the_socket_basename(self):
        # Every display of a session compositor is lifted to /run under its own
        # name, so wayland-1 does not collide with wayland-0.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=["/run/user/1000/wayland-0",
                                        "/run/user/1000/wayland-1"])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "display"))["display"]["containerEdits"]
            self.assertEqual(
                [(m["hostPath"], m["containerPath"]) for m in edits["mounts"]],
                [("/run/user/1000/wayland-0", "/run/wayland-0"),
                 ("/run/user/1000/wayland-1", "/run/wayland-1")])

    def test_two_logins_both_reach_the_display_spec(self):
        # The two-user host, greeter included: every login's displays are
        # mounted, the second session's under a suffixed mount point, and the
        # greeter's is left out. The spec is what a container is handed, so
        # this is the end of the chain the unit tests cover in pieces.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=["/run/user/123/wayland-0",
                                        "/run/user/1000/wayland-0",
                                        "/run/user/1000/wayland-1",
                                        "/run/user/1001/wayland-0",
                                        "/run/user/1001/wayland-1"])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "display"))["display"]["containerEdits"]
            self.assertEqual(
                [(m["hostPath"], m["containerPath"]) for m in edits["mounts"]],
                [("/run/user/1000/wayland-0", "/run/wayland-0"),
                 ("/run/user/1000/wayland-1", "/run/wayland-1"),
                 ("/run/user/1001/wayland-0", "/run/wayland-0-1"),
                 ("/run/user/1001/wayland-1", "/run/wayland-1-1")])

    def test_system_audio_socket_is_mounted_where_found(self):
        # A system-wide PulseAudio on /run/pulse/native is already where a
        # container client looks for it.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SYSTEM_SOCKETS["audio"]])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "audio"))["audio"]["containerEdits"]
            self.assertEqual(edits["mounts"][0]["hostPath"], "/run/pulse/native")
            self.assertEqual(edits["mounts"][0]["containerPath"],
                             "/run/pulse/native")

    def test_audio_socket_of_another_uid_is_remapped_to_run(self):
        # The host session may run under any uid; the container always sees
        # /run/pulse/native, and never the host's uid.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=["/run/user/1001/pulse/native"])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "audio"))["audio"]["containerEdits"]
            self.assertEqual(edits["mounts"][0]["hostPath"],
                             "/run/user/1001/pulse/native")
            self.assertEqual(edits["mounts"][0]["containerPath"],
                             "/run/pulse/native")

    def test_per_user_sockets_are_the_fallback(self):
        # With nothing under /run itself, the search falls back to the per-user
        # runtime directory and finds the session server there.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["audio"],
                                                   FAKE_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            for cdiclass in ("audio", "display"):
                edits = self._devices(self._spec(d, cdiclass))[cdiclass]["containerEdits"]
                self.assertEqual([m["hostPath"] for m in edits["mounts"]],
                                 [FAKE_SOCKETS[cdiclass]])
                # Found in the host's runtime dir, handed over at the /run
                # path: the container side is the same whichever layout and
                # whichever uid the socket was found under.
                self.assertEqual(edits["mounts"][0]["containerPath"],
                                 CONTAINER_SOCKETS[cdiclass])

    def test_system_socket_wins_when_both_layouts_exist(self):
        # Both present: only the system-wide one is mounted, so a single
        # compositor is not handed to the container under two paths.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d],
                               sockets=[FAKE_SOCKETS["display"],
                                        FAKE_SYSTEM_SOCKETS["display"]])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "display"))["display"]["containerEdits"]
            self.assertEqual([m["hostPath"] for m in edits["mounts"]],
                             [FAKE_SYSTEM_SOCKETS["display"]])
            # Either socket would have been handed over at the same container
            # path, so which one wins changes only the host side.
            self.assertEqual([m["containerPath"] for m in edits["mounts"]],
                             [CONTAINER_SOCKETS["display"]])

    def test_per_user_socket_found_under_any_uid(self):
        # The runtime dir is globbed rather than pinned to uid 1000, so a
        # target running its compositor as another user is still picked up.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=["/run/user/1234/wayland-0"])
            self.assertEqual(rc, 0)
            edits = self._devices(self._spec(d, "display"))["display"]["containerEdits"]
            self.assertEqual([m["hostPath"] for m in edits["mounts"]],
                             ["/run/user/1234/wayland-0"])
            # The host uid is normalised away, exactly as it is for audio: the
            # container never sees which user the session belonged to.
            self.assertEqual(edits["mounts"][0]["containerPath"],
                             "/run/wayland-0")

    def test_no_generated_spec_mounts_under_run_user(self):
        # The invariant across every class and host layout: the container side
        # of a mount never names a per-user runtime directory, since nothing in
        # the container resolves one.
        layouts = (list(FAKE_SOCKETS.values()),
                   list(FAKE_SYSTEM_SOCKETS.values()),
                   ["/run/user/1234/wayland-0", "/run/user/1234/pulse/native"])
        for sockets in layouts:
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(run_generator(["-d", d], sockets=sockets), 0)
                for path in (Path(d) / "run" / "cdi").glob("*.json"):
                    spec = json.loads(path.read_text())
                    mounts = [m for dev in spec["devices"]
                              for m in dev["containerEdits"].get("mounts", [])]
                    mounts += spec["containerEdits"].get("mounts", [])
                    for mount in mounts:
                        self.assertFalse(
                            mount["containerPath"].startswith("/run/user/"),
                            "%s: %s" % (path.name, mount))

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

    def test_camera_socket_group_reaches_additional_gids(self):
        # The camera class has only its socket to read a group from.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=[FAKE_SOCKETS["camera"]],
                               node_gids={FAKE_SOCKETS["camera"]: 1002})
            self.assertEqual(rc, 0)
            self.assertEqual(
                self._spec(d, "camera")["containerEdits"]["additionalGids"],
                [1002])

    def test_all_classes_written_on_a_full_board(self):
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=FAKE_SOCKETS.values())
            self.assertEqual(rc, 0)
            self.assertEqual(self._written(d), FAKE_CDI_FILES)

    def test_new_classes_are_selectable(self):
        # --classes has to accept the new names, and scope the output to them.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-C", "audio,display"],
                               sockets=FAKE_SOCKETS.values())
            self.assertEqual(rc, 0)
            self.assertEqual(self._written(d), ["qualcomm-audio.json",
                                                "qualcomm-display.json"])

    def test_the_gdsp_class_is_selectable(self):
        # The gdsp nodes are a class of their own, so they can be asked for
        # without the other two DSPs coming along.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "-C", "fastrpc-gdsp"])
            self.assertEqual(rc, 0)
            self.assertEqual(self._written(d), ["qualcomm-fastrpc-gdsp.json"])
            spec = self._spec(d, "fastrpc-gdsp")
            self.assertEqual(spec["kind"], "qualcomm.com/fastrpc-gdsp")
            self.assertEqual(
                sorted(device["name"] for device in spec["devices"]),
                ["fastrpc-gdsp0", "fastrpc-gdsp1", "fastrpc-gdsp:all"])


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
            # Mirror how main() writes a spec, so these validations exercise
            # the same on-disk formatting the generator really produces.
            json.dump(spec, f, indent=gen.JSON_INDENT)
            f.write("\n")
        return path

    def _assert_valid(self, spec_dir):
        rc, out = self._validate_dir(spec_dir)
        self.assertEqual(rc, 0, "cdi validate reported errors:\n%s" % out)

    def test_gpu_spec_validates(self):
        nodes = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
        devices = gen.generate_devicenodes_cdi("renderD", nodes)
        # No env entry, as no class but the fastrpc ones carries one.
        spec = gen.build_cdi_spec("gpu", devices,
                                   gen.generate_enventries_cdi("gpu",
                                                               FAKE_MACHINE),
                                   [], gen.node_additional_gids("gpu"))
        self.assertNotIn("env", spec["containerEdits"])
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "gpu", spec)
            self._assert_valid(d)

    def test_v4l2_spec_validates(self):
        nodes = ["/dev/video%d" % i for i in range(5)]
        devices = gen.generate_devicenodes_cdi("video", nodes)
        spec = gen.build_cdi_spec("v4l2", devices, [], [],
                                   gen.node_additional_gids("v4l2"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "v4l2", spec)
            self._assert_valid(d)

    def test_dmaheap_spec_validates(self):
        devices = gen.generate_devicenodes_cdi("dmaheap-system",
                                               ["/dev/dma_heap/system"])
        spec = gen.build_cdi_spec("dmaheap", devices, [], [],
                                   gen.node_additional_gids("dmaheap"))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "dmaheap", spec)
            self._assert_valid(d)

    def test_fastrpc_spec_with_mounts_validates(self):
        nodes = ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"]
        devices = gen.generate_devicenodes_cdi("fastrpc-cdsp", nodes)
        mounts = [{"hostPath": "/usr/share/foo/bar/1.0/aarch64/dsp/cdsp/fastrpc_shell_3",
                   "containerPath": "/usr/share/hexagon-dsp/cdsp/fastrpc_shell_3",
                   "options": list(gen.DSP_FILE_MOUNT_OPTIONS)},
                  {"hostPath": DEVICETREE_MODEL,
                   "containerPath": DEVICE_MODEL_MOUNTPOINT,
                   "options": ["rbind", "ro"]}]
        # The fullest shape a generated spec takes: device nodes, mounts, a
        # group and the board name, all in one file.
        spec = gen.build_cdi_spec("fastrpc-cdsp", devices,
                                   gen.generate_enventries_cdi("fastrpc-cdsp",
                                                               FAKE_MACHINE),
                                   mounts,
                                   gen.node_additional_gids("fastrpc-cdsp"))
        self.assertEqual(spec["containerEdits"]["env"],
                         ["MACHINE_NAME=" + FAKE_MACHINE])
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
        # The multi-glob GPU class: several device names in one spec, and the
        # two groups its nodes span in additionalGids.
        devices = (gen.generate_devicenodes_cdi("renderD", ["/dev/dri/renderD128"])
                   + gen.generate_devicenodes_cdi("card", ["/dev/dri/card0"],
                                                  catchall=False)
                   + gen.generate_devicenodes_cdi("kgsl-3d", ["/dev/kgsl-3d0"],
                                                  catchall=False)
                   + gen.generate_devicenodes_cdi(
                       "gpu", ["/dev/dri/renderD128", "/dev/dri/card0", "/dev/kgsl-3d0"],
                       pernode=False))
        spec = gen.build_cdi_spec("gpu", devices, [], [],
                                  gen.node_additional_gids("gpu", [44, 110]))
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "gpu", spec)
            self._assert_valid(d)

    def test_socket_only_spec_validates(self):
        # A device whose containerEdits carry only mounts and no deviceNodes is
        # a shape the other classes never produce; confirm the schema accepts it.
        for cdiclass, socket in (("camera", "/run/cam_server/le_camera_socket"),
                                 ("audio", "/run/user/1000/pulse/native"),
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
        # The board name is presented too, so every validated file carries the
        # env entry and the fastrpc ones the model node mount as well.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d], sockets=FAKE_SOCKETS.values(),
                               devicetree=FAKE_MACHINE)

            self.assertEqual(rc, 0)
            cdi_dir = Path(d) / "run" / "cdi"
            written = sorted(p.name for p in cdi_dir.glob("*.json"))
            self.assertEqual(written, FAKE_CDI_FILES)
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
