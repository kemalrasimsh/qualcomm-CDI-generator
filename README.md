# Qualcomm CDI generator

Tooling to generate CDI config files to pass devices through to containers

The generated files conform to the [Container Device Interface (CDI)
specification](https://github.com/cncf-tags/container-device-interface) from
the CNCF, so they can be consumed by any CDI-aware container runtime and
validated with the upstream `cdi` tool (see [Validating generated CDI
files](#validating-generated-cdi-files)).

## Branches

**main**: Primary development branch. Contributors should develop submissions based on this branch, and submit pull requests to this branch.

## Requirements

The main tool only requires python3 to generate a CDI. To consume it, you need a container runtime like podman or docker.

### Minimum runtime versions

The generated specs declare `cdiVersion` `0.7.0`, because that is the first CDI
version defining `additionalGids` (see [Device node ownership and
permissions](#device-node-ownership-and-permissions)). A runtime built against
an older CDI library rejects the spec with:

```
the spec version must be at least v0.7.0
```

So a runtime that bundles CDI `specs-go` v0.7.0 or later is required:

| Runtime | Minimum version | Reason |
|---------|-----------------|--------|
| Docker  | **26.1.0** | first release vendoring CDI `specs-go` v0.7.0; 26.0.x vendors v0.6.0 and rejects these specs |
| Podman  | **5.1.0** | first release vendoring CDI `specs-go` v0.7.0; 5.0.x and 4.x vendor v0.6.0 |

Docker 29.x vendors CDI `specs-go` v1.1.0 and needs no configuration: CDI is
enabled by default and `/etc/cdi` and `/var/run/cdi` are scanned out of the box,
so the generated files are picked up as soon as they are written.

```shell
$ docker --version
Docker version v29.3.0, build df016a3a9
```

Podman 5.6.x and Docker 28.3.x likewise work out of the box. For Docker
26.1.x–27.x you need a `/etc/docker/daemon.json` file:

```json
{
  "features": {
     "cdi": true
  },
  "cdi-spec-dirs": ["/etc/cdi/", "/run/cdi"]
}
```

For Docker 28.0.x only the feature enablement is needed:
```json
{
  "features": {
     "cdi": true
  }
}
```

Either restart the docker daemon or reboot to have this config take effect.

> **Note:** On Docker 29.x the `cdi` feature flag is opt-*out* — it only needs to
> appear in `daemon.json` to switch CDI off (`"cdi": false`).

> **Note:** Docker 26.1.x shows the configured `CDI spec directories` in `docker info` but does not list `Discovered Devices`. Devices are still available for use with `--device`; the "Discovered Devices" section only appears in Docker 28.x and later.

## Installation Instructions

Copy over `qualcomm-cdi-generator.py`

## Usage

On the target, run the CDI generator as root. The output directory `/run/cdi` is created automatically:

```shell
# qualcomm-cdi-generator.py
```

The tool probes the hardware and writes one CDI JSON file per device class under `/run/cdi`. It writes nothing outside that directory and makes no changes to the host device nodes, so it works on targets whose platform filesystem is mounted read-only. Run with `-v` to see what was found and written, or `-vv` for full debug output.

### Command-line options

| Option | Long form | Default | Description |
|--------|-----------|---------|-------------|
| `-d` | `--destdir` | `/` | Root directory for all output paths |
| `-c` | `--cdifilename` | `qualcomm.json` | CDI filename base; the device class is inserted before the extension, e.g. `qualcomm.json` → `qualcomm-gpu.json` |
| `-C` | `--classes` | all | Comma-separated list of CDI classes to generate. Available: `gpu`, `v4l2`, `dmaheap`, `fastrpc-cdsp`, `fastrpc-adsp` |
| `-m` | `--filemode` | `0660` | `fileMode` for generated device nodes, parsed as octal |
| `-u` | `--uid` | `0` | `uid` for generated device nodes |
| `-g` | `--gid` | from host nodes | `gid` for generated device nodes and `additionalGids`, overriding the group read from each host device node |
| `-n` | `--dry-run` | off | Probe devices but do not write any files |
| `-v` | `--verbose` | off | Increase verbosity; use `-vv` for debug output |

### Device node ownership and permissions

Every generated `deviceNodes` entry carries `fileMode`, `uid` and `gid`, which
the container runtime applies when it creates the node inside the container.
These describe the access the container should get, not the host node's current
state, so the generated spec is self-contained and does not depend on the host
permissions at generation time.

Because the nodes are exposed group accessible rather than world accessible, the
container process has to be a member of the owning group to open them. Each
generated spec therefore also sets `containerEdits.additionalGids` to those
groups, so the runtime adds them to the container's supplementary groups.

Access is expressed entirely within the CDI spec. The generator does not install
a `createContainer` hook and never changes the permissions of the host device
nodes.

The group is read from the host device node, because that is the group the
driver and its udev rules actually hand the device to, and it varies per board —
a render node is `render` on one target and the container user's primary group
on another. Reading it keeps the spec matched to the hardware it was generated
on instead of a compiled-in guess. The generator only reads the node's group; it
does not change it.

The group is resolved **per node**, not per class, because one class can span
several groups on a single board — a target may expose `/dev/video32` in `video`
while a camera node alongside it belongs to a vendor group:

```
INFO: Groups for 'v4l2' nodes: 44 (30 node(s)), 1002 (2 node(s))
```

Each `deviceNodes` entry then carries the group that owns that node, and the
spec grants every one of them:

```json
  "containerEdits": {
    "additionalGids": [ 44, 1002 ]
  }
```

`additionalGids` is a spec-level array while `gid` is per node, so granting only
one group would leave exactly the nodes that differ unopenable inside the
container.

A node falls back to a built-in per-class default when it has no group of its
own — it could not be read, or it is owned by group `root` (a `root:root` node
has no group to join, and the runtime drops a `0` entry from `additionalGids`,
so honouring it would grant an unprivileged container nothing):

| Class | Fallback `gid` | Rationale |
|-------|----------------|-----------|
| `gpu` | 1000 | render node handed to the container user's primary group |
| `v4l2` | 44 | V4L2 nodes conventionally belong to the `video` group |
| `dmaheap` | 1000 | DMA heap handed to the container user's primary group |
| `fastrpc-cdsp` | 1000 | Hexagon DSP node handed to the container user's primary group |
| `fastrpc-adsp` | 1000 | Hexagon DSP node handed to the container user's primary group |

Nodes are exposed root-owned and group accessible (`0660`, `uid` 0). Override
any of that for all classes at once:

```shell
# qualcomm-cdi-generator.py --filemode 660 --uid 0 --gid 1000
```

`--gid` replaces the group read from the host nodes for every node and class, so
the spec grants exactly that one group. It sets both the node `gid` and
`additionalGids`, keeping the groups the container joins in sync with the groups
that own the nodes.

`--filemode` is read as octal, so `660` and `0660` both mean `rw-rw----`. Note
that JSON has no octal literal, so the emitted value is the decimal equivalent:
`0660` appears in the file as `432`.

### Example session

```shell
(cdi) root@ventunoq:~# qualcomm-cdi-generator.py -v
INFO: Starting Qualcomm CDI generation
INFO: Config: destdir=/, cdifilename=qualcomm.json, dry_run=False
INFO: Node attributes: fileMode=0o660 (default), uid=0 (default), gid=from host device nodes
INFO: Found 1 nodes for pattern /dev/dri/renderD*
INFO: Group for 'gpu' nodes: 1000
INFO: Generating CDI entries for 'renderD' with 1 node(s)
INFO: Found 30 nodes for pattern /dev/video*
INFO: Group for 'v4l2' nodes: 44
INFO: Generating CDI entries for 'video' with 30 node(s)
INFO: Found 1 nodes for pattern /dev/dma_heap/*system
INFO: Group for 'dmaheap' nodes: 1000
INFO: Generating CDI entries for 'dmaheap-system' with 1 node(s)
INFO: Found 2 nodes for pattern /dev/fastrpc-cdsp*
INFO: Group for 'fastrpc-cdsp' nodes: 1000
INFO: Generating CDI entries for 'fastrpc-cdsp' with 2 node(s)
INFO: Found 1 nodes for pattern /dev/fastrpc-adsp*
INFO: Group for 'fastrpc-adsp' nodes: 1000
INFO: Generating CDI entries for 'fastrpc-adsp' with 1 node(s)
INFO: Detected Arduino Monza from devicetree
INFO: Found 14 nodes for pattern /usr/share/*/*/*/*/dsp/
WARNING: Old style monolithic CDI detected, removing /run/cdi/qualcomm.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-gpu.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-v4l2.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-dmaheap.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-cdsp.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-adsp.json
INFO: Completed Qualcomm CDI generation
(cdi) root@ventunoq:~# docker info
[...]
Server:
 CDI spec directories:
  /etc/cdi
  /var/run/cdi
 Discovered Devices:
  cdi: qualcomm.com/dmaheap=dmaheap-system
  cdi: qualcomm.com/dmaheap=dmaheap-system:all
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  cdi: qualcomm.com/gpu=renderD128
  cdi: qualcomm.com/gpu=renderD:all
  cdi: qualcomm.com/v4l2=video0
  cdi: qualcomm.com/v4l2=video1
  cdi: qualcomm.com/v4l2=video10
  cdi: qualcomm.com/v4l2=video11
  cdi: qualcomm.com/v4l2=video12
  cdi: qualcomm.com/v4l2=video13
  cdi: qualcomm.com/v4l2=video14
  cdi: qualcomm.com/v4l2=video15
  cdi: qualcomm.com/v4l2=video16
  cdi: qualcomm.com/v4l2=video17
  cdi: qualcomm.com/v4l2=video18
  cdi: qualcomm.com/v4l2=video19
  cdi: qualcomm.com/v4l2=video2
  cdi: qualcomm.com/v4l2=video20
  cdi: qualcomm.com/v4l2=video21
  cdi: qualcomm.com/v4l2=video22
  cdi: qualcomm.com/v4l2=video23
  cdi: qualcomm.com/v4l2=video24
  cdi: qualcomm.com/v4l2=video25
  cdi: qualcomm.com/v4l2=video26
  cdi: qualcomm.com/v4l2=video27
  cdi: qualcomm.com/v4l2=video28
  cdi: qualcomm.com/v4l2=video29
  cdi: qualcomm.com/v4l2=video3
  cdi: qualcomm.com/v4l2=video4
  cdi: qualcomm.com/v4l2=video5
  cdi: qualcomm.com/v4l2=video6
  cdi: qualcomm.com/v4l2=video7
  cdi: qualcomm.com/v4l2=video8
  cdi: qualcomm.com/v4l2=video9
  cdi: qualcomm.com/v4l2=video:all
[...]
(cdi) root@ventunoq:~# cdi classes
CDI device classes found:
  0. dmaheap (1 vendors: qualcomm.com)
  1. fastrpc-adsp (1 vendors: qualcomm.com)
  2. fastrpc-cdsp (1 vendors: qualcomm.com)
  3. gpu (1 vendors: qualcomm.com)
  4. v4l2 (1 vendors: qualcomm.com)
(cdi) root@ventunoq:~# cdi devices
CDI devices found:
  0. qualcomm.com/dmaheap=dmaheap-system
  1. qualcomm.com/dmaheap=dmaheap-system:all
  2. qualcomm.com/fastrpc-adsp=fastrpc-adsp
  3. qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  4. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  5. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  6. qualcomm.com/gpu=renderD128
  7. qualcomm.com/gpu=renderD:all
  8. qualcomm.com/v4l2=video0
  9. qualcomm.com/v4l2=video1
  10. qualcomm.com/v4l2=video10
  11. qualcomm.com/v4l2=video11
  12. qualcomm.com/v4l2=video12
  13. qualcomm.com/v4l2=video13
  14. qualcomm.com/v4l2=video14
  15. qualcomm.com/v4l2=video15
  16. qualcomm.com/v4l2=video16
  17. qualcomm.com/v4l2=video17
  18. qualcomm.com/v4l2=video18
  19. qualcomm.com/v4l2=video19
  20. qualcomm.com/v4l2=video2
  21. qualcomm.com/v4l2=video20
  22. qualcomm.com/v4l2=video21
  23. qualcomm.com/v4l2=video22
  24. qualcomm.com/v4l2=video23
  25. qualcomm.com/v4l2=video24
  26. qualcomm.com/v4l2=video25
  27. qualcomm.com/v4l2=video26
  28. qualcomm.com/v4l2=video27
  29. qualcomm.com/v4l2=video28
  30. qualcomm.com/v4l2=video29
  31. qualcomm.com/v4l2=video3
  32. qualcomm.com/v4l2=video4
  33. qualcomm.com/v4l2=video5
  34. qualcomm.com/v4l2=video6
  35. qualcomm.com/v4l2=video7
  36. qualcomm.com/v4l2=video8
  37. qualcomm.com/v4l2=video9
  38. qualcomm.com/v4l2=video:all
```

You can then pass one or more of the above entries to the runtime:
```shell
(cdi) root@ventunoq:~# docker run --network host --device qualcomm.com/gpu=renderD128 --device=qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all --rm -it aiml-container:latest /bin/bash
```
When using a CDI device, the container will automatically get additional metadata, for example the `MACHINE_NAME` environment variable:
```shell
(cdi) root@ventunoq:~# docker run --network host --device=qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all --rm -it aiml-container:latest /bin/bash
root@container:/# env | grep MACHINE
MACHINE_NAME=Arduino Monza
```

To generate only the fastrpc classes:
```shell
(cdi) root@ventunoq:~# qualcomm-cdi-generator.py --classes fastrpc-cdsp,fastrpc-adsp -v
INFO: Starting Qualcomm CDI generation
INFO: Config: destdir=/, cdifilename=qualcomm.json, dry_run=False
INFO: Node attributes: fileMode=0o660 (default), uid=0 (default), gid=from host device nodes
INFO: Found 1 nodes for pattern /dev/dri/renderD*
INFO: Group for 'gpu' nodes: 1000
INFO: Generating CDI entries for 'renderD' with 1 node(s)
INFO: Found 30 nodes for pattern /dev/video*
INFO: Group for 'v4l2' nodes: 44
INFO: Generating CDI entries for 'video' with 30 node(s)
INFO: Found 1 nodes for pattern /dev/dma_heap/*system
INFO: Group for 'dmaheap' nodes: 1000
INFO: Generating CDI entries for 'dmaheap-system' with 1 node(s)
INFO: Found 2 nodes for pattern /dev/fastrpc-cdsp*
INFO: Group for 'fastrpc-cdsp' nodes: 1000
INFO: Generating CDI entries for 'fastrpc-cdsp' with 2 node(s)
INFO: Found 1 nodes for pattern /dev/fastrpc-adsp*
INFO: Group for 'fastrpc-adsp' nodes: 1000
INFO: Generating CDI entries for 'fastrpc-adsp' with 1 node(s)
INFO: Detected Arduino Monza from devicetree
INFO: Found 14 nodes for pattern /usr/share/*/*/*/*/dsp/
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-cdsp.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-adsp.json
INFO: Completed Qualcomm CDI generation
```

### CDI file structure

The tool writes one JSON file per device class. Each file has a `kind` matching its class (e.g. `qualcomm.com/gpu`). Every `deviceNodes` entry carries `fileMode`, `uid` and the `gid` read from that host device node, and the spec-level `containerEdits` sets `additionalGids` to every group those nodes use (see [Device node ownership and permissions](#device-node-ownership-and-permissions)). The `fastrpc` files additionally include bind-mounts for Hexagon DSP firmware found under `/usr/share/*/*/*/*/dsp/` and the devicetree model string, since those binaries are tightly coupled to the in-kernel firmware loader.

The specs declare `cdiVersion` `0.7.0`, the first CDI version that defines
`additionalGids`; see [Minimum runtime versions](#minimum-runtime-versions) for
the runtime releases that accept it.

Example `qualcomm-gpu.json`:
```json
{
  "cdiVersion": "0.7.0",
  "kind": "qualcomm.com/gpu",
  "devices": [
    {
      "name": "renderD128",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/renderD128", "fileMode": 432, "uid": 0, "gid": 1000 }
        ]
      }
    },
    {
      "name": "renderD:all",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/renderD128", "fileMode": 432, "uid": 0, "gid": 1000 }
        ]
      }
    }
  ],
  "containerEdits": {
    "env": [ "MACHINE_NAME=Qualcomm Technologies, Inc. Robotics RB3gen2" ],
    "additionalGids": [ 1000 ]
  }
}
```

`fileMode` is `432`, the decimal form of octal `0660`.

## Validating generated CDI files

The generated JSON files follow the [CNCF Container Device Interface
specification](https://github.com/cncf-tags/container-device-interface), whose
`cdi` command-line tool can validate spec files against the CDI JSON schema.

Point `cdi validate` at a directory of spec files with `--spec-dirs` (it
scans `/etc/cdi` and `/var/run/cdi` by default) and it exits non-zero on any
load or schema error. Validation checks only the spec structure — the
referenced device nodes need not exist on the validating machine:

```shell
$ cdi validate --spec-dirs /run/cdi
No CDI Registry errors.
```

## Running the tests

The stand-alone `unittest` suite under [tests/](tests/) exercises the
spec-building helpers and validates generated CDI files with the `cdi` tool.
It needs only the Python standard library; the validation tests additionally
need `cdi`, located via the `CDI_TOOL` environment variable or `PATH` and
skipped when it is not found.

```shell
python3 -m unittest discover -s tests -v
CDI_TOOL=/path/to/cdi python3 -m unittest discover -s tests -v
```

## Continuous Integration

Project CI runs the unit tests, CDI validation, package builds, static checks,
and LAVA job schema checks. Pushes to `main` and the daily hardware workflow
also flash a
[qcom-deb-images](https://github.com/qualcomm-linux/qcom-deb-images) build on
`qrb2210-rb1`, `qcs6490-rb3gen2-vision-kit`, and `glymur-crd` boards in the
shared Foundries.io LAVA lab, then run the generator from the commit under
test.

Image selection uses valid publications from completed trusted `build.yml`
runs on `main`, accepting scheduled and legacy `workflow_run` triggers.
Upstream board-test failures do not disqualify published images. The
three-month freshness limit, successful default image build for the requested
suite, and live immutable publication pointer remain mandatory. Automatic
selection tries older fresh runs when a candidate fails publication validation;
an explicitly requested run must pass without fallback.

See [.github/workflows/Readme.md](.github/workflows/Readme.md) for the
workflow list and [ci/lava-tests/cdi-generate/](ci/lava-tests/cdi-generate/)
for the on-target test assets.

## Development

How to develop new features/fixes for the software. Maybe different than "usage". Also provide details on how to contribute via a [CONTRIBUTING.md file](CONTRIBUTING.md).

## Getting in Contact

How to contact maintainers. E.g. GitHub Issues, GitHub Discussions could be indicated for many cases. However a mail list or list of Maintainer e-mails could be shared for other types of discussions. E.g.

* [Report an Issue on GitHub](../../issues)

## License

*Qualcomm CDI generator* is licensed under the [BSD-3-clause License](https://spdx.org/licenses/BSD-3-Clause.html). See [LICENSE.txt](LICENSE.txt) for the full license text.
