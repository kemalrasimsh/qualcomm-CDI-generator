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
    "/dev/video*": ["/dev/video0", "/dev/video1"],
    "/dev/dma_heap/*system": ["/dev/dma_heap/system"],
    "/dev/fastrpc-cdsp*": ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"],
    "/dev/fastrpc-adsp*": ["/dev/fastrpc-adsp"],
    "/usr/share/*/*/*/*/dsp/": [],
}


def run_generator(extra_argv, fake_nodes=FAKE_NODES):
    """Run main() with synthetic device discovery into a fresh argv.

    find_devicenodes is replaced with controlled node lists and the direct
    glob.glob() devicetree probe is stubbed empty, so the run does not depend
    on the host hardware. Returns main()'s exit code.
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

    with mock.patch.object(gen, "find_devicenodes", fake_find), \
            mock.patch.object(gen.glob, "glob", fake_glob), \
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
        spec = gen.build_cdi_spec("gpu", [], "vendorhook", [], [])
        self.assertEqual(spec["cdiVersion"], gen.CDI_VERSION)
        self.assertEqual(spec["kind"], gen.CDI_VENDOR + "/gpu")

    def test_hook_is_added(self):
        spec = gen.build_cdi_spec("gpu", [], "myhook", [], [])
        hooks = spec["containerEdits"]["hooks"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["hookName"], "createContainer")
        self.assertEqual(hooks[0]["path"], "/bin/myhook")

    def test_mounts_only_for_fastrpc(self):
        mounts = [{"hostPath": "/x", "containerPath": "/x", "options": ["ro", "bind"]}]
        gpu = gen.build_cdi_spec("gpu", [], "vendorhook", [], mounts)
        self.assertNotIn("mounts", gpu["containerEdits"])
        cdsp = gen.build_cdi_spec("fastrpc-cdsp", [], "vendorhook", [], mounts)
        self.assertEqual(cdsp["containerEdits"]["mounts"], mounts)

    def test_mount_none_placeholders_filtered(self):
        # main() pre-allocates the mount list with None placeholders; build_cdi_spec
        # must strip them so the emitted spec never contains null mount entries.
        mounts = [None, {"hostPath": "/x", "containerPath": "/x", "options": ["ro", "bind"]}, None]
        cdsp = gen.build_cdi_spec("fastrpc-cdsp", [], "vendorhook", [], mounts)
        self.assertEqual(len(cdsp["containerEdits"]["mounts"]), 1)
        self.assertNotIn(None, cdsp["containerEdits"]["mounts"])

    def test_env_passthrough(self):
        env = ["MACHINE_NAME=Synthetic Board"]
        spec = gen.build_cdi_spec("fastrpc-adsp", [], "vendorhook", env, [])
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
        # End to end: a default run must emit the attributes with the per-class
        # gid, and the JSON must round-trip fileMode as a decimal integer (the
        # JSON grammar has no octal literal).
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

    def _hook_nodes(self, destdir):
        """Return the list of node paths chmod'd by the generated hook script."""
        text = (Path(destdir) / "bin" / "vendorhook").read_text()
        for line in text.splitlines():
            if line.startswith("for node in"):
                # "for node in <paths...> ; do"
                return line[len("for node in"):].split(";")[0].split()
        return []

    def test_classes_filters_hook_nodes(self):
        # --classes must scope the hook's chmod list to the selected classes,
        # keeping the hook in sync with the generated CDI files.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d, "--classes", "fastrpc-cdsp"])
            self.assertEqual(rc, 0)
            nodes = self._hook_nodes(d)
            self.assertEqual(sorted(nodes),
                             ["/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure"])

    def test_default_hook_includes_all_nodes(self):
        # Without --classes the hook covers every discovered node.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d])
            self.assertEqual(rc, 0)
            nodes = set(self._hook_nodes(d))
            self.assertEqual(nodes, {
                "/dev/dri/renderD128",
                "/dev/video0", "/dev/video1",
                "/dev/dma_heap/system",
                "/dev/fastrpc-cdsp", "/dev/fastrpc-cdsp-secure",
                "/dev/fastrpc-adsp",
            })


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
        spec = gen.build_cdi_spec("gpu", devices, "vendorhook", [], [])
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "gpu", spec)
            self._assert_valid(d)

    def test_v4l2_spec_validates(self):
        nodes = ["/dev/video%d" % i for i in range(5)]
        devices = gen.generate_devicenodes_cdi("video", nodes,
                                               gen.node_permissions("v4l2"))
        spec = gen.build_cdi_spec("v4l2", devices, "vendorhook", [], [])
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "v4l2", spec)
            self._assert_valid(d)

    def test_dmaheap_spec_validates(self):
        devices = gen.generate_devicenodes_cdi("dmaheap-system", ["/dev/dma_heap/system"],
                                               gen.node_permissions("dmaheap"))
        spec = gen.build_cdi_spec("dmaheap", devices, "vendorhook", [], [])
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
        spec = gen.build_cdi_spec("fastrpc-cdsp", devices, "vendorhook", env, mounts)
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "fastrpc-cdsp", spec)
            self._assert_valid(d)

    def test_fastrpc_spec_with_empty_mounts_validates(self):
        # When no DSP firmware is found the generator still emits "mounts": [].
        devices = gen.generate_devicenodes_cdi("fastrpc-adsp", ["/dev/fastrpc-adsp"])
        spec = gen.build_cdi_spec("fastrpc-adsp", devices, "vendorhook", [], [])
        with tempfile.TemporaryDirectory() as d:
            self._write_spec(d, "fastrpc-adsp", spec)
            self._assert_valid(d)

    def test_full_generator_run_validates(self):
        # Drive main() end to end with synthetic device discovery so every class
        # is produced, then validate the whole set of written files together.
        with tempfile.TemporaryDirectory() as d:
            rc = run_generator(["-d", d])

            self.assertEqual(rc, 0)
            cdi_dir = Path(d) / "run" / "cdi"
            written = sorted(p.name for p in cdi_dir.glob("*.json"))
            self.assertEqual(written, [
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
