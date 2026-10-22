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
| `-C` | `--classes` | all | Comma-separated list of CDI classes to generate. Available: `gpu`, `v4l2`, `camera`, `audio`, `display`, `dmaheap`, `fastrpc-cdsp`, `fastrpc-adsp` |
| `-m` | `--filemode` | `0660` | `fileMode` for generated device nodes, parsed as octal |
| `-u` | `--uid` | `0` | `uid` for generated device nodes |
| `-g` | `--gid` | from host nodes | `gid` for generated device nodes and `additionalGids`, overriding the group read from each host device node |
| `-n` | `--dry-run` | off | Probe devices but do not write any files |
| `-v` | `--verbose` | off | Increase verbosity; use `-vv` for debug output |

### Device classes

Each class becomes one CDI file and is written only when the hardware or
host-side server it describes is actually present:

| Class | Device nodes | Mounts |
|-------|--------------|--------|
| `gpu` | `/dev/dri/renderD*`, `/dev/dri/card*`, `/dev/kgsl-3d*` | — |
| `v4l2` | `/dev/video*` (all of them) | — |
| `camera` | — | `/run/cam_server/le_camera_socket` |
| `audio` | — | `/run/pulse/native`, else `/run/user/*/pulse/native` |
| `display` | — | `/run/wayland-*`, else `/run/user/*/wayland-*` |
| `dmaheap` | `/dev/dma_heap/*system` | — |
| `fastrpc-cdsp` | `/dev/fastrpc-cdsp*` | DSP firmware, devicetree model |
| `fastrpc-adsp` | `/dev/fastrpc-adsp*` | DSP firmware, devicetree model |

#### GPU nodes

The `gpu` class exposes the render node a container draws through, the DRM card
node, and the Adreno KGSL node when the kernel provides one — some downstream
kernels expose `/dev/kgsl-3d0` instead of routing everything through DRM. A
board without it simply gets the render and card nodes.

Each node is available under its own device name, and `gpu:all` hands over the
whole stack at once. The historical `renderD128` / `renderD:all` names are kept
unchanged, so specs and scripts that already reference them keep working:

```shell
# docker run --device qualcomm.com/gpu=gpu:all --rm -it mycontainer
```

Note that the GPU nodes frequently span two groups — the render node belongs to
`render` while the card node belongs to `video` — which is handled per node; see
[Device node ownership and permissions](#device-node-ownership-and-permissions).

#### Socket-backed classes

`camera`, `audio` and `display` pass through no device node. The container talks
to the host camera server (qmmf/CamX), PulseAudio server and Wayland compositor
over a unix socket, so the socket *is* the device and the spec carries a
bind-mount instead of a `deviceNodes` entry. `v4l2` still exposes every
`/dev/video*` node, encoders and decoders included, for a client that wants to
drive V4L2 directly.

Each socket is bind-mounted read-write — a socket is useless to a client that
cannot write to it — and `nosuid,noexec`. It is normally mounted at the same
path inside the container, so the usual client defaults (`PULSE_SERVER`,
`WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`) resolve unchanged; the one exception is
the system-wide Wayland socket, see [Where the sockets are
mounted](#where-the-sockets-are-mounted).

##### Where the sockets are looked for

`audio` and `display` can be laid out two ways on a target, so each is searched
for in order and the **first pattern that matches anything wins**:

| Class | Preferred (system-wide) | Fallback (per-user session) |
|-------|-------------------------|-----------------------------|
| `audio` | `/run/pulse/native` | `/run/user/*/pulse/native` |
| `display` | `/run/wayland-*` | `/run/user/*/wayland-*` |

A system-wide sound server or compositor puts its socket directly under `/run`,
which is checked first; a session one puts it in the user runtime directory,
which is the fallback. The runtime directory is globbed rather than pinned to
uid 1000, so a target running these servers as another user is still picked up.

Stopping at the first match matters: if both layouts are present, only the
system-wide socket is mounted, rather than handing the container the same
compositor twice under two different paths. Within the winning pattern every
match is kept, so a compositor exposing `wayland-0` and `wayland-1` passes both
through.

`camera` has one location, `/run/cam_server/le_camera_socket`.

##### Where the sockets are mounted

The container-side path is not always the host path: the audio socket is pinned
to one location, and a system-wide Wayland socket is relocated into the
container's user runtime directory.

| Host path | Container path |
|-----------|----------------|
| `/run/pulse/native` | `/run/user/1000/pulse/native` |
| `/run/user/*/pulse/native` | `/run/user/1000/pulse/native` |
| `/run/wayland-0` | `/run/user/1000/wayland-0` |
| `/run/user/*/wayland-*` | unchanged |
| `/run/cam_server/le_camera_socket` | unchanged |

**Audio** always appears at `/run/user/1000/pulse/native`, whatever the host
layout — system-wide, or a session under any uid. The container therefore sees
one stable path and does not have to be told which uid the host session happens
to run as.

**Wayland** resolves its socket relative to `XDG_RUNTIME_DIR`, so a compositor
listening on `/run/wayland-0` would be invisible to an unconfigured client if it
were mounted there verbatim; relocating it to `/run/user/1000/wayland-0` puts it
where the client already looks. The basename is preserved, so `/run/wayland-1`
becomes `/run/user/1000/wayland-1` and the displays do not collide. A Wayland
socket already in a runtime directory is mounted exactly where it was found,
uid and all — unlike audio, it is not pinned to 1000.

The `1000` in these paths is the **container's** user, not the host's — it is
the uid the stock images run as, and the container filesystem is where the path
is created. The host side always keeps whatever path the socket was actually
found at, whichever uid owns it. Adjust `SOCKET_CONTAINER_DIRS` in the generator
for an image that runs as a different user.

A class whose socket is missing is skipped, exactly as a class whose device
nodes are missing is: the host-side server is not running, so the spec would
hand the container a path it cannot connect to. The socket's owning group is
read and granted the same way a device node's is, since the container has to be
in that group to connect.

```shell
# docker run --device qualcomm.com/camera=camera --device qualcomm.com/audio=audio \
      --device qualcomm.com/display=display --rm -it mycontainer
```

> **Note:** a target that puts these sockets somewhere neither pattern covers
> needs `CLASS_SOCKETS` in the generator adjusted.

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
while a node alongside it belongs to a vendor group:

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
| `camera` | 1000 | camera socket in the runtime dir, owned by that user's group |
| `audio` | 1000 | audio socket in the user runtime dir, owned by that user's group |
| `display` | 1000 | Wayland socket in the user runtime dir, likewise |
| `dmaheap` | 1000 | DMA heap handed to the container user's primary group |
| `fastrpc-cdsp` | 1000 | Hexagon DSP node handed to the container user's primary group |
| `fastrpc-adsp` | 1000 | Hexagon DSP node handed to the container user's primary group |

For the socket-backed classes the group is read from the socket rather than a
device node; everything else about the resolution is identical.

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
INFO: Found 1 nodes for pattern /dev/dri/card*
INFO: Found 1 nodes for pattern /dev/kgsl-3d*
INFO: Groups for 'gpu' nodes: 44 (1 path(s)), 110 (2 path(s))
INFO: Generating CDI entries for 'renderD' with 1 node(s)
INFO: Generating CDI entries for 'card' with 1 node(s)
INFO: Generating CDI entries for 'kgsl-3d' with 1 node(s)
INFO: Generating CDI entries for 'gpu' with 3 node(s)
INFO: Found 30 nodes for pattern /dev/video*
INFO: Group for 'v4l2' nodes: 44
INFO: Generating CDI entries for 'video' with 30 node(s)
INFO: Found camera socket: /run/cam_server/le_camera_socket
INFO: Group for 'camera' sockets: 1002
INFO: Generating CDI entries for 'camera' with 1 socket(s)
INFO: Found audio socket: /run/user/1000/pulse/native
INFO: Group for 'audio' sockets: 29
INFO: Generating CDI entries for 'audio' with 1 socket(s)
INFO: Found display socket: /run/user/1000/wayland-0
INFO: Group for 'display' sockets: 1000
INFO: Generating CDI entries for 'display' with 1 socket(s)
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
INFO: Wrote CDI JSON: /run/cdi/qualcomm-camera.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-audio.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-display.json
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
  cdi: qualcomm.com/audio=audio
  cdi: qualcomm.com/audio=audio:all
  cdi: qualcomm.com/camera=camera
  cdi: qualcomm.com/camera=camera:all
  cdi: qualcomm.com/display=display
  cdi: qualcomm.com/display=display:all
  cdi: qualcomm.com/dmaheap=dmaheap-system
  cdi: qualcomm.com/dmaheap=dmaheap-system:all
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  cdi: qualcomm.com/gpu=card0
  cdi: qualcomm.com/gpu=gpu:all
  cdi: qualcomm.com/gpu=kgsl-3d0
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
  0. audio (1 vendors: qualcomm.com)
  1. camera (1 vendors: qualcomm.com)
  2. display (1 vendors: qualcomm.com)
  3. dmaheap (1 vendors: qualcomm.com)
  4. fastrpc-adsp (1 vendors: qualcomm.com)
  5. fastrpc-cdsp (1 vendors: qualcomm.com)
  6. gpu (1 vendors: qualcomm.com)
  7. v4l2 (1 vendors: qualcomm.com)
(cdi) root@ventunoq:~# cdi devices
CDI devices found:
  0. qualcomm.com/audio=audio
  1. qualcomm.com/audio=audio:all
  2. qualcomm.com/camera=camera
  3. qualcomm.com/camera=camera:all
  4. qualcomm.com/display=display
  5. qualcomm.com/display=display:all
  6. qualcomm.com/dmaheap=dmaheap-system
  7. qualcomm.com/dmaheap=dmaheap-system:all
  8. qualcomm.com/fastrpc-adsp=fastrpc-adsp
  9. qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  10. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  11. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  12. qualcomm.com/gpu=card0
  13. qualcomm.com/gpu=gpu:all
  14. qualcomm.com/gpu=kgsl-3d0
  15. qualcomm.com/gpu=renderD128
  16. qualcomm.com/gpu=renderD:all
  17. qualcomm.com/v4l2=video0
  18. qualcomm.com/v4l2=video1
  19. qualcomm.com/v4l2=video10
  20. qualcomm.com/v4l2=video11
  21. qualcomm.com/v4l2=video12
  22. qualcomm.com/v4l2=video13
  23. qualcomm.com/v4l2=video14
  24. qualcomm.com/v4l2=video15
  25. qualcomm.com/v4l2=video16
  26. qualcomm.com/v4l2=video17
  27. qualcomm.com/v4l2=video18
  28. qualcomm.com/v4l2=video19
  29. qualcomm.com/v4l2=video2
  30. qualcomm.com/v4l2=video20
  31. qualcomm.com/v4l2=video21
  32. qualcomm.com/v4l2=video22
  33. qualcomm.com/v4l2=video23
  34. qualcomm.com/v4l2=video24
  35. qualcomm.com/v4l2=video25
  36. qualcomm.com/v4l2=video26
  37. qualcomm.com/v4l2=video27
  38. qualcomm.com/v4l2=video28
  39. qualcomm.com/v4l2=video29
  40. qualcomm.com/v4l2=video3
  41. qualcomm.com/v4l2=video4
  42. qualcomm.com/v4l2=video5
  43. qualcomm.com/v4l2=video6
  44. qualcomm.com/v4l2=video7
  45. qualcomm.com/v4l2=video8
  46. qualcomm.com/v4l2=video9
  47. qualcomm.com/v4l2=video:all
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
INFO: Found 1 nodes for pattern /dev/dri/card*
INFO: Found 1 nodes for pattern /dev/kgsl-3d*
INFO: Groups for 'gpu' nodes: 44 (1 path(s)), 110 (2 path(s))
INFO: Generating CDI entries for 'renderD' with 1 node(s)
INFO: Generating CDI entries for 'card' with 1 node(s)
INFO: Generating CDI entries for 'kgsl-3d' with 1 node(s)
INFO: Generating CDI entries for 'gpu' with 3 node(s)
INFO: Found 30 nodes for pattern /dev/video*
INFO: Group for 'v4l2' nodes: 44
INFO: Generating CDI entries for 'video' with 30 node(s)
INFO: Found camera socket: /run/cam_server/le_camera_socket
INFO: Group for 'camera' sockets: 1002
INFO: Generating CDI entries for 'camera' with 1 socket(s)
INFO: Found audio socket: /run/user/1000/pulse/native
INFO: Group for 'audio' sockets: 29
INFO: Generating CDI entries for 'audio' with 1 socket(s)
INFO: Found display socket: /run/user/1000/wayland-0
INFO: Group for 'display' sockets: 1000
INFO: Generating CDI entries for 'display' with 1 socket(s)
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

Note that every class is still probed; `--classes` selects which CDI files are
written, not what the generator looks at.

### CDI file structure

The tool writes one JSON file per device class (see [Device
classes](#device-classes)). Each file has a `kind` matching its class (e.g. `qualcomm.com/gpu`). Every `deviceNodes` entry carries `fileMode`, `uid` and the `gid` read from that host device node, and the spec-level `containerEdits` sets `additionalGids` to every group those nodes use (see [Device node ownership and permissions](#device-node-ownership-and-permissions)). The `fastrpc` files additionally include bind-mounts for Hexagon DSP firmware found under `/usr/share/*/*/*/*/dsp/` and the devicetree model string, since those binaries are tightly coupled to the in-kernel firmware loader.

The specs declare `cdiVersion` `0.7.0`, the first CDI version that defines
`additionalGids`; see [Minimum runtime versions](#minimum-runtime-versions) for
the runtime releases that accept it.

The files are written formatted — two-space indentation, one trailing newline —
so they can be read and diffed in place on target when a board does not expose
what was expected. Runtimes parse this identically to a compact spec. The
examples below match that structure, with the short string arrays folded onto
one line for brevity; the generator puts each element on its own line.

Example `qualcomm-gpu.json`, on a board whose render node is in `render` (110)
and card node in `video` (44):
```json
{
  "cdiVersion": "0.7.0",
  "kind": "qualcomm.com/gpu",
  "devices": [
    {
      "name": "renderD128",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/renderD128", "fileMode": 432, "uid": 0, "gid": 110 }
        ]
      }
    },
    {
      "name": "renderD:all",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/renderD128", "fileMode": 432, "uid": 0, "gid": 110 }
        ]
      }
    },
    {
      "name": "card0",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/card0", "fileMode": 432, "uid": 0, "gid": 44 }
        ]
      }
    },
    {
      "name": "gpu:all",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/card0", "fileMode": 432, "uid": 0, "gid": 44 },
          { "path": "/dev/dri/renderD128", "fileMode": 432, "uid": 0, "gid": 110 }
        ]
      }
    }
  ],
  "containerEdits": {
    "env": [ "MACHINE_NAME=Qualcomm Technologies, Inc. Robotics RB3gen2" ],
    "additionalGids": [ 44, 110 ]
  }
}
```

A `/dev/kgsl-3d0` node, where present, appears alongside `card0` under its own
`kgsl-3d0` name and in `gpu:all`.

Example `qualcomm-audio.json`, where the socket mount rides on the device entry
so that requesting the device is what mounts the socket. This host runs its
audio session under uid 1001, so `hostPath` keeps that uid while
`containerPath` is pinned to 1000 (see [Where the sockets are
mounted](#where-the-sockets-are-mounted)):
```json
{
  "cdiVersion": "0.7.0",
  "kind": "qualcomm.com/audio",
  "devices": [
    {
      "name": "audio",
      "containerEdits": {
        "mounts": [
          {
            "hostPath": "/run/user/1001/pulse/native",
            "containerPath": "/run/user/1000/pulse/native",
            "options": [ "nosuid", "noexec", "rw", "bind" ]
          }
        ]
      }
    }
  ],
  "containerEdits": {
    "env": [ "MACHINE_NAME=Qualcomm Technologies, Inc. Robotics RB3gen2" ],
    "additionalGids": [ 1001 ]
  }
}
```

Note that `additionalGids` follows the **host** socket's owning group (1001
here), not the container-side uid: the container has to join the group that owns
the socket it is actually connecting to.

The `audio:all` entry is omitted above for brevity; it carries the same mount,
so both spellings behave alike.

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
