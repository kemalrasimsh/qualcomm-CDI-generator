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

The main tool requires python3 and [PyYAML](https://pyyaml.org/) to generate a CDI. To consume it, you need a container runtime like podman or docker.

PyYAML is the one dependency outside the standard library: the board's DSP
configuration is a YAML file and is read as one (see [Which board's
firmware](#which-boards-firmware)). The import is unconditional, so a host
without it writes no specs at all — install `python3-yaml` on a Debian/Ubuntu
image, or `PyYAML` from pip. An image that ships the hexagon-dsp configuration
the generator reads normally carries a parser for it already.

### Minimum runtime versions

The generated specs declare `cdiVersion` `0.7.0`, because that is the first CDI
version defining `additionalGids` (see [Group
access](#group-access)). A runtime built against
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
| `-c` | `--cdifilename` | `qualcomm.json` | CDI filename base for the `qualcomm.com` specs; the device class is inserted before the extension, e.g. `qualcomm.json` → `qualcomm-gpu.json`. A spec in another vendor namespace is named after that vendor instead, e.g. `linux-audio.json` (see [Device classes](#device-classes)) |
| `-C` | `--classes` | all | Comma-separated list of CDI classes to generate. Available: `gpu`, `v4l2`, `camera`, `audio`, `display`, `dmaheap`, `fastrpc-cdsp`, `fastrpc-adsp`, `fastrpc-gdsp` |
| `-g` | `--gid` | from host paths | `gid` to grant via `additionalGids`, replacing the group read from each host device node and socket |
| `-n` | `--dry-run` | off | Probe devices but do not write any files |
| `-v` | `--verbose` | off | Increase verbosity; use `-vv` for debug output |

### Device classes

Each class becomes one CDI file and is written only when the hardware or
host-side server it describes is actually present:

> **Note:** no class depends on another. A host with no DSP node at all (none of
> `/dev/fastrpc-cdsp*`, `/dev/fastrpc-adsp*` or `/dev/fastrpc-gdsp*`) gets no
> `fastrpc` file and still gets its `gpu`, `v4l2`, `camera`, `audio`, `display`
> and `dmaheap` ones — each describes what it found and nothing else.

| Class | Kind | Device nodes | Mounts |
|-------|------|--------------|--------|
| `gpu` | `qualcomm.com/gpu` | `/dev/dri/renderD*`, `/dev/dri/card*`, `/dev/kgsl-3d*` | — |
| `v4l2` | `linux.org/v4l2` | `/dev/video*` (all of them) | — |
| `camera` | `qualcomm.com/camera` | — | `/run/cam_server/le_camera_socket` |
| `audio` | `linux.org/audio` | — | `/run/pulse/native`, else `/run/user/*/pulse/native` |
| `display` | `linux.org/display` | — | `/run/wayland-*`, else `/run/user/*/wayland-*` |
| `dmaheap` | `qualcomm.com/dmaheap` | `/dev/dma_heap/*system` | — |
| `fastrpc-cdsp` | `qualcomm.com/fastrpc-cdsp` | `/dev/fastrpc-cdsp*` | [DSP firmware](#dsp-firmware-mounts), [board name](#board-name) |
| `fastrpc-adsp` | `qualcomm.com/fastrpc-adsp` | `/dev/fastrpc-adsp*` | [DSP firmware](#dsp-firmware-mounts), [board name](#board-name) |
| `fastrpc-gdsp` | `qualcomm.com/fastrpc-gdsp` | `/dev/fastrpc-gdsp*` | [DSP firmware](#dsp-firmware-mounts), [board name](#board-name) |

Three classes are in the `linux.org` namespace rather than `qualcomm.com`: the
`v4l2` nodes are the kernel's own video interface, and `audio` and `display`
hand over a PulseAudio socket and a Wayland socket. Nothing about those three
is Qualcomm specific — the same spec describes the same resources on any Linux
host running those servers. A spec file is named after the vendor its kind is
in, so they are written as `linux-v4l2.json`, `linux-audio.json` and
`linux-display.json` beside the `qualcomm-*.json` of the hardware classes, and
a container asks for them as `--device linux.org/display=display`.

The three `fastrpc` rows also set `MACHINE_NAME` to the board name, the mounted
devicetree node being the second way the same name is handed over; no other
class sets a variable, see [Board name](#board-name).

Each `fastrpc` class is the nodes of one DSP and the firmware of that DSP
alone: `fastrpc-cdsp` the compute DSP, `fastrpc-adsp` the audio DSP and
`fastrpc-gdsp` the DSP the host's `gdsprpcd` serves, exposed as
`/dev/fastrpc-gdsp0` and `/dev/fastrpc-gdsp1` on a board that has it (an
SA8775P). A board without one of those DSPs gets no file for it, as with every
other class.

#### GPU nodes

The `gpu` class exposes the render node a container draws through, the DRM card
node, and the Adreno KGSL node when the kernel provides one — some downstream
kernels expose `/dev/kgsl-3d0` instead of routing everything through DRM. A
board without it simply gets the render and card nodes.

Each node is available under its own device name, and `gpu:all` hands over the
whole stack at once. The render nodes are named after the nodes themselves
(`renderD128`), while their catch-all is `render:all`, named after the node class
rather than the `renderD` prefix the nodes are spelled with:

```shell
# docker run --device qualcomm.com/gpu=gpu:all --rm -it mycontainer
```

Note that the GPU nodes frequently span two groups — the render node belongs to
`render` while the card node belongs to `video` — which is handled per node; see
[Group access](#group-access).

#### DMA heap names

A target can expose the system heap under the plain name, the vendor-qualified
devicetree spelling, or both:

| Host node | Device name |
|-----------|-------------|
| `/dev/dma_heap/system` | `dmaheap-system` |
| `/dev/dma_heap/qcom,system` | `dmaheap-system-qcom` |

A node whose name is qualified with a vendor is named after that vendor, so the
device says which heap it is. A positional name (`dmaheap-system0`) would not,
and would move as soon as a node appears or goes away. The plain node keeps the
bare `dmaheap-system` name whether or not the vendor one is there beside it,
and `dmaheap-system:all` hands over every heap found.

#### Socket-backed classes

`camera`, `audio` and `display` pass through no device node. The container talks
to the host camera server (qmmf/CamX), PulseAudio server and Wayland compositor
over a unix socket, so the socket *is* the device and the spec carries a
bind-mount instead of a `deviceNodes` entry. `v4l2` still exposes every
`/dev/video*` node, encoders and decoders included, for a client that wants to
drive V4L2 directly.

Each socket is bind-mounted read-write — a socket is useless to a client that
cannot write to it — and `nosuid,noexec`. Inside the container every socket
appears at its system-wide location under `/run`, a fixed path a client is
pointed at in full (`WAYLAND_DISPLAY=/run/wayland-0`,
`PULSE_SERVER=unix:/run/pulse/native`); a socket the host keeps in a per-user
runtime directory is lifted up to that path, see [Where the sockets are
mounted](#where-the-sockets-are-mounted). The
generator mounts the socket but does not set those variables, see [The
container environment](#the-container-environment).

`audio` and `display` name **every socket they pass through as a device of its
own** — `audio0`, `display0` and upwards, in the order the sockets were found —
because a host can run several of each, one per session, and a container asking
for a display is asking for one of them, not for every display on the machine.
A class with a single socket, which is every ordinary target, names it after
the class itself, so `linux.org/display=display` is still what a stock image
asks for. `display:all` and `audio:all` carry every socket of the class at
once. `camera` has one system-wide socket, so the class name is already the
name of the only device there is.

##### The container environment

**`MACHINE_NAME` is the only variable a generated spec sets, and only the
three `fastrpc` specs set it** — see [Board name](#board-name). No other spec sets
anything: what those classes pass through is a path, a device node or a socket,
and the variable naming that path belongs to the image and the `docker run`
command line.

A client therefore has to be pointed at what the spec mounted. The stock images
already resolve the mounted paths, so nothing extra is needed on an ordinary
target; an image that does not, or one whose entrypoint clears the environment,
needs the paths on the command line — otherwise it gets a mounted socket its
client never looks at:

| Variable | Value to pass |
|----------|---------------|
| `WAYLAND_DISPLAY` | `/run/wayland-0` |
| `PULSE_SERVER` | `unix:/run/pulse/native` |

Each value names the mounted socket in full:

```shell
# docker run --device linux.org/display=display \
#     --env WAYLAND_DISPLAY=/run/wayland-0 ...
```

A Wayland client given an absolute `WAYLAND_DISPLAY` connects to exactly that
socket and does not consult `XDG_RUNTIME_DIR` at all, so the display takes one
variable rather than two. **Do not override `XDG_RUNTIME_DIR`** to make a
relative `WAYLAND_DISPLAY=wayland-0` resolve: setting it to `/run` moves the
runtime directory for everything else in the image that looks a path up through
it, and buys nothing the absolute form does not already give.

The same applies to a board whose compositor serves a differently numbered
socket: it keeps that basename through the mount, so the container sees
`/run/wayland-1` and wants `WAYLAND_DISPLAY=/run/wayland-1`. The mount itself
is correct either way. A host with a second user logged in has that user's
displays mounted under a suffixed name (`/run/wayland-0-1`), and a container
meant for that session has to be pointed at it; the same holds for a second
audio socket, see [Two sockets wanting one container
path](#two-sockets-wanting-one-container-path).

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

What a pattern matches that is not a socket is dropped first. A compositor keeps
a lock file next to every socket it serves, so `/run/user/*/wayland-*` also
matches `/run/user/1000/wayland-1.lock`; mounting that would hand the container
a path no client connects to, and the host compositor's lock to take. Any
`.lock` match is therefore left out, and a runtime directory holding nothing but
a stale lock file does not count as a match at all — the next pattern still gets
its turn. Adjust `SOCKET_IGNORE_SUFFIXES` in the generator to drop more.

`camera` has one location, `/run/cam_server/le_camera_socket`.

##### Every login's displays and sound servers

`display` and `audio` are both served per session, and both collide on the way
in. A Wayland socket is numbered **per runtime directory, not per machine** — a
compositor calls its first socket `wayland-0` whatever any other compositor on
the host has called its own — and a PulseAudio socket is the constant `native`
in every session. A target with two users logged in next to a display manager
therefore has:

```
/run/user/123/wayland-0        /run/user/123/pulse/native     (the greeter's)
/run/user/1000/wayland-0       /run/user/1000/pulse/native    (the first user's)
/run/user/1000/wayland-1
/run/user/1001/wayland-0       /run/user/1001/pulse/native    (the second user's)
/run/user/1001/wayland-1
```

**Every regular login's sockets are passed through** — two users logged in at
once means two compositors and two sound servers worth reaching, and the
container is handed both rather than whichever one the generator preferred.
Within a session the compositor does its own numbering, so `wayland-0` and
`wayland-1` keep the numbers the host gave them; between sessions the sockets
ask for one container path and the later one is given a suffixed mount point,
see [Two sockets wanting one container
path](#two-sockets-wanting-one-container-path):

| Host path | Container path | Device |
|-----------|----------------|--------|
| `/run/user/1000/wayland-0` | `/run/wayland-0` | `display0` |
| `/run/user/1000/wayland-1` | `/run/wayland-1` | `display1` |
| `/run/user/1001/wayland-0` | `/run/wayland-0-1` | `display2` |
| `/run/user/1001/wayland-1` | `/run/wayland-1-1` | `display3` |
| `/run/user/1000/pulse/native` | `/run/pulse/native` | `audio0` |
| `/run/user/1001/pulse/native` | `/run/pulse/native-1` | `audio1` |

Each is a device of its own, so a container is handed the one session's
compositor or sound server it asks for rather than all of them; `display:all`
and `audio:all` still carry the lot. The index is a position in the spec and
not a session number — `hostPath` is what says which session a mount came from.

**A system account's session is dropped**, the greeter's above being the one
most targets have: its compositor is not what a container is meant to draw on,
nor its sound server what a container is meant to play through, and passing
either through would spend the unsuffixed name on it. `SESSION_UID_MIN` (1000)
is the floor, and the sockets left out are logged:

```
INFO: Skipping 1 display socket(s) in /run/user/123: uid 123 is a system account, not a login of uid 1000 or above
INFO: Skipping 1 audio socket(s) in /run/user/123/pulse: uid 123 is a system account, not a login of uid 1000 or above
```

The uid is only ranked with, never pinned, so a target whose users run as
something other than 1000 is handled the same way, and a host running nothing
but system sessions keeps them after all — those servers are the only ones
there are. Sockets are mounted **lowest session uid first**, so which session
keeps the unsuffixed `/run/wayland-<n>` or `/run/pulse/native` does not depend
on which one came up first.

Each session's socket brings its own owning group, and the spec grants all of
them, so a container can connect to whichever one it is pointed at; see [Group
access](#group-access).

Both classes are filtered this way (`SOCKET_LOGIN_SESSION_CLASSES` in the
generator). `camera` is not: its server has one system-wide socket and no
sessions to tell apart.

##### Where the sockets are mounted

Every socket is handed to the container at its **system-wide location under
`/run`**, whatever the host layout. Nothing in the container resolves
`/run/user/<uid>`: a client there is pointed at `/run/wayland-0` and
`/run/pulse/native`, the paths the stock images already resolve.

| Host path | Container path |
|-----------|----------------|
| `/run/pulse/native` | unchanged |
| `/run/user/*/pulse/native` | `/run/pulse/native` |
| `/run/wayland-*` | unchanged |
| `/run/user/*/wayland-*` | `/run/wayland-*` |
| `/run/cam_server/le_camera_socket` | unchanged |

A host that already serves the socket from `/run` needs no remapping — both
sides of the mount name the same path. Only the per-user layouts are rewritten,
to lift the socket out of the session directory. The container side therefore
never reveals which user the host session belonged to: a target with one
session running as 1001 produces the same spec as one running as 1000, and
where two sessions want one path the later mount point takes a counted suffix
rather than a uid (see below).

The basename is preserved, so a second display of one session on
`/run/user/1000/wayland-1` lands on `/run/wayland-1` and the two do not
collide. Two *sessions* do collide, since each compositor numbers from zero;
see below.

##### Two sockets wanting one container path

The rewrite keeps only the basename, so two host sockets can ask for the same
container path, and a host with more than one session has that both ways:

* an audio socket's basename is the constant `native`, and the uid was all that
  told `/run/user/1000/pulse/native` and `/run/user/1001/pulse/native` apart,
* a display socket is numbered by its own compositor from zero, so
  `/run/user/1000/wayland-0` and `/run/user/1001/wayland-0` both ask for
  `/run/wayland-0`.

**Both are suffixed** `-1`, `-2`, … on the mount point, so no session's socket
is dropped, none overrides another, and every one of them still sits in the
directory its client looks in. Which sockets are separated this way is
`SOCKET_COLLISION_SUFFIXED` in the generator:

| Host path | Container path |
|-----------|----------------|
| `/run/user/1000/pulse/native` | `/run/pulse/native` |
| `/run/user/1001/pulse/native` | `/run/pulse/native-1` |
| `/run/user/1000/wayland-0` | `/run/wayland-0` |
| `/run/user/1001/wayland-0` | `/run/wayland-0-1` |

Only a regular login's sockets get this far, a system account's having been
dropped before the mounts are built (see [Every login's displays and sound
servers](#every-logins-displays-and-sound-servers)).

Every name assigned is logged:

```
INFO: Container path /run/wayland-0 is already mounted, using /run/wayland-0-1
```

Display sockets are taken lowest session uid first and audio sockets in sorted
order, so which one goes unsuffixed is stable across runs. It is the unsuffixed
path that lets a stock image connect with no configuration; a container that
wants one of the others is pointed at it with
`PULSE_SERVER=unix:/run/pulse/native-1` or
`WAYLAND_DISPLAY=/run/wayland-0-1`. The counter also walks past a path already
taken, so a host genuinely serving `native-1` is not collided with.

> **Note:** the suffix is a disambiguator, **not a session number**. The counter
> runs per container path, so a session whose `wayland-1` collides with nothing
> keeps the bare `/run/wayland-1` while its `wayland-0` is suffixed. The `-1` in
> `/run/wayland-0-1` and in `/run/pulse/native-1` need not be the same session
> either. The spec's `hostPath` is what says which session a mount came from.

A socket matching none of those patterns — the camera server's — keeps the
container path it was given. No layout the generator globs puts two of them on
one path, since one host path is globbed once, but should it ever happen the
spec carries both mounts and the later one shadows the earlier, so only one
socket is reachable. The run then warns which path was taken twice and which
host socket won it, rather than leaving that to be guessed at.

A single-session host — every ordinary target — has nothing to separate, so its
socket is simply mounted at `/run/pulse/native` or `/run/wayland-0`.

The per-user runtime directory is not handed over as a directory, which is what
makes this a path rewrite rather than one mount: it cannot be placed on `/run`
without shadowing the container's own `/run`. Adjust `SOCKET_CONTAINER_DIRS` in
the generator for an image that resolves its sockets somewhere else.

A class whose socket is missing is skipped, exactly as a class whose device
nodes are missing is: the host-side server is not running, so the spec would
hand the container a path it cannot connect to. The socket's owning group is
read and granted the same way a device node's is, since the container has to be
in that group to connect.

```shell
# docker run --device qualcomm.com/camera=camera --device linux.org/audio=audio \
      --device linux.org/display=display --rm -it mycontainer
```

> **Note:** a target that puts these sockets somewhere neither pattern covers
> needs `CLASS_SOCKETS` in the generator adjusted.

#### DSP firmware mounts

The three `fastrpc` classes carry bind-mounts for the Hexagon DSP firmware on
top of their device nodes, because those binaries are tightly coupled to the
files the in-kernel firmware loader loads; mounting the host's copies keeps the
container from running a mismatched pair.

**Only the firmware files themselves are mounted** — the host directory they
were found in is not. A spec carries the handful of files its own FastRPC
client opens, each under `/usr/share/hexagon-dsp/`, plus the board
configuration that says where they came from.

##### Which board's firmware

The board says so itself. The hexagon-dsp packaging installs a YAML file per
board family in a `conf.d`, naming a firmware directory per devicetree model:

```yaml
# /usr/share/qcom/conf.d/hexagon-dsp-binaries-thundercomm-rb3gen2.yaml
machines:
  Qualcomm Technologies, Inc. Robotics RB3gen2:
    DSP_LIBRARY_PATH: qcm6490/Thundercomm/RB3gen2/dsp
```

Both locations are searched, in this order — the first is where the
qualcomm-linux images install it and the second where the package of that name
does:

| Configuration directory | `DSP_LIBRARY_PATH` is relative to |
|-------------------------|-----------------------------------|
| `/usr/share/qcom/conf.d` | `/usr/share/qcom` |
| `/usr/share/hexagon-dsp/conf.d` | `/usr/share/hexagon-dsp` |

The board name read off the devicetree (see [Board name](#board-name)) picks
the entry, and `DSP_LIBRARY_PATH` resolved against the directory holding that
`conf.d` is the firmware directory. For the file above that is
`/usr/share/qcom/qcm6490/Thundercomm/RB3gen2/dsp`.

**That `conf.d` is bind-mounted into the container at
`/usr/share/hexagon-dsp/conf.d`**, read-only, whichever of the two locations the
host keeps it in:

| Host path | Container path |
|-----------|----------------|
| `/usr/share/qcom/conf.d` | `/usr/share/hexagon-dsp/conf.d` |
| `/usr/share/hexagon-dsp/conf.d` | unchanged |

So a container resolves one path for the configuration as it does for the
firmware, and needs to know nothing about the host's layout. It is the one
directory a `fastrpc` spec mounts, and whatever resolves `DSP_LIBRARY_PATH`
inside the container reads the same file the generator read rather than a copy
the image shipped for some other board. That container path is the `conf.d` of
the directory the firmware itself is presented in — `DSP_CONF_CONTAINER_DIR` is
built from `DSP_FIRMWARE_CONTAINER_DIR` in the generator — so the
configuration and the firmware it describes cannot drift apart.

Where a host has **both** locations, the one holding the file that named this
board is the one handed over — only one can be, both arriving at a single
container path, and the container needs the file its own board is in. If
nothing named the board, the first in search order is passed through; there is
nothing to choose between them then.

The file is read with PyYAML (see [Requirements](#requirements)), so the SPDX
header, the schema annotation and any other top-level section cost nothing, and
quoting and indentation are the parser's to resolve. Only the one shape above
is taken from the document — the `machines` mapping, its model keys and their
settings — and it is read with `yaml.safe_load()`, the file being a board
description to read and not code to run.

A file that cannot be read, is not valid YAML, or is not shaped that way names
no board. None of the three stops the run: the firmware is resolvable from the
host's own symlinks as well, so a broken configuration costs the second-best
answer and not the whole generation.

```
WARNING: Cannot parse the DSP configuration /usr/share/qcom/conf.d/hexagon-dsp-binaries.yaml: while parsing a flow node
```

**An image carrying no configuration** falls back to the symlinks the rpcd
services leave behind. `cdsprpcd`, `adsprpcd` and `gdsprpcd` each symlink one
board's `dsp/<dsp>/*` into `/usr/lib/dsp/<dsp>/` on startup, so resolving
`/usr/lib/dsp/*/*` leads back to the directory they picked — the same answer,
one step removed. A target that installs real files there instead of symlinks
resolves to those files, which says the same thing.

**A host where neither answers gets no firmware mounts**, and nothing is
looked for past them. Its `fastrpc` specs are still written — the device nodes
and the board name do not depend on the firmware — and the run says so:

```
WARNING: Neither the DSP configuration nor /usr/lib/dsp names a firmware directory for 'Arduino Monza'; the fastrpc specs carry no firmware
```

That is deliberate. Looking for a firmware directory nothing pointed at means,
on an image built for a family of targets, finding another board's — and
firmware built for a different SoC is worse than no firmware, being exactly the
mismatch mounting the host's copies is meant to avoid. The same rule holds once
a board *has* answered: a class whose firmware is missing from the resolved
directory is a DSP the image installed none for, and no other directory is
substituted for it.

The run says which way it went and what it resolved:

```
INFO: Found DSP configuration directory: /usr/share/qcom/conf.d
INFO: /usr/share/qcom/conf.d/hexagon-dsp-binaries-thundercomm-rb3gen2.yaml sets DSP_LIBRARY_PATH=qcm6490/Thundercomm/RB3gen2/dsp for 'Qualcomm Technologies, Inc. Robotics RB3gen2'
INFO: Resolving 'fastrpc-cdsp' firmware from /usr/share/qcom/qcm6490/Thundercomm/RB3gen2/dsp
INFO: Mounting DSP configuration directory /usr/share/qcom/conf.d into the container at /usr/share/hexagon-dsp/conf.d
```

or, on an image with no configuration:

```
INFO: No DSP configuration directory (/usr/share/qcom/conf.d, /usr/share/hexagon-dsp/conf.d)
INFO: /usr/lib/dsp/cdsp/fastrpc_shell_3 resolves the DSP firmware directory /usr/share/qcom/qcm6490/Thundercomm/RB3gen2/dsp
```

Adjust `DSP_CONF_DIRS` in the generator for an image that keeps the
configuration somewhere else.

##### Which files are mounted

The files the FastRPC client opens are mounted under
**`/usr/share/hexagon-dsp/`**, one fixed location whatever board-specific
directory the host kept them in, each keeping its per-DSP sub-path and nothing
else of where it was found. They are per DSP, so each class is given its own
and no spec carries another's:

| Class | File | Mounted at | Options |
|-------|------|------------|---------|
| `fastrpc-cdsp` | `cdsp*/fastrpc_shell_*` | `/usr/share/hexagon-dsp/cdsp*/fastrpc_shell_*` | `rw` |
| `fastrpc-cdsp` | `cdsp*/libc++.so.1` | `/usr/share/hexagon-dsp/cdsp*/libc++.so.1` | `ro` |
| `fastrpc-cdsp` | `cdsp*/libc++abi.so.1` | `/usr/share/hexagon-dsp/cdsp*/libc++abi.so.1` | `ro` |
| `fastrpc-adsp` | `adsp*/fastrpc_shell_*` | `/usr/share/hexagon-dsp/adsp*/fastrpc_shell_*` | `rw` |
| `fastrpc-adsp` | `adsp*/libc++.so.1` | `/usr/share/hexagon-dsp/adsp*/libc++.so.1` | `ro` |
| `fastrpc-adsp` | `adsp*/libc++abi.so.1` | `/usr/share/hexagon-dsp/adsp*/libc++abi.so.1` | `ro` |
| `fastrpc-gdsp` | `gdsp*/fastrpc_shell_*` | `/usr/share/hexagon-dsp/gdsp*/fastrpc_shell_*` | `rw` |
| `fastrpc-gdsp` | `gdsp*/libc++.so.1` | `/usr/share/hexagon-dsp/gdsp*/libc++.so.1` | `ro` |
| `fastrpc-gdsp` | `gdsp*/libc++abi.so.1` | `/usr/share/hexagon-dsp/gdsp*/libc++abi.so.1` | `ro` |

These are the files the rpcd services symlink into `/usr/lib/dsp/<dsp>/` on the
host, reached by a bind-mount instead because the container image carries no DSP
firmware of its own.

The sub-path and basename are preserved, so `fastrpc_shell_3` and
`fastrpc_shell_unsigned_3` both appear under their own name. The `*` after the
DSP name is a wildcard for the multi-DSP targets, which keep one firmware
directory per DSP: an SA8775P carries `cdsp/fastrpc_shell_3` alongside
`cdsp1/fastrpc_shell_4`, matching its `/dev/fastrpc-cdsp` and
`/dev/fastrpc-cdsp1` nodes, and both land under their own sub-path of
`/usr/share/hexagon-dsp/`.

The Hexagon shell is mounted read-write, as the hand-maintained specs these
replace have it. The C++ runtime beside it is mounted read-only: `libc++.so.1`
and `libc++abi.so.1` are libraries the container loads and never writes, and
the file behind the mount is the host's own copy. Every one of them is
`nosuid`, and the options come from the glob that matched the file rather than
from the directory they share.

A file with no match is skipped, so a board carrying only the unsigned shell
gets only that. A class with no firmware at all gets no firmware mount, and
its spec is still written for the device nodes and the board name.

They are looked for in the directory the board resolved to. Where that is
several candidates — symlinks resolving to more than one directory — the first
one holding this class's firmware is where all of it comes from, two mounts on
one container path being a conflicting spec; a candidate holding none of it is
passed over rather than ending the search.

Adjust `DSP_FIRMWARE_FILES` in the generator for a target whose FastRPC stack
resolves more files by path, and `DSP_FIRMWARE_CONTAINER_DIR` for an image that
looks for them somewhere other than `/usr/share/hexagon-dsp`.

##### Board name

The three `fastrpc` specs are the only ones that hand over the board name, and
they hand it over two ways: as the `MACHINE_NAME` variable they set, and as the
devicetree model node they bind-mount. The FastRPC library derives the name to
resolve its Hexagon binary paths, taking it from the variable when it is set and
reading the node itself otherwise, so a client is served whichever way it looks.

The generator reads the name off the host devicetree — the same read that picks
the board's entry in the DSP configuration, see [Which board's
firmware](#which-boards-firmware) — and sets it on each of those specs:

```json
  "containerEdits": {
    "env": [
      "MACHINE_NAME=Arduino Monza"
    ]
```

No other class sets it. Nothing in them resolves a path against a board name,
and a variable in their specs would be exported into every container that asks
for a GPU or a camera — a reach into the image's environment that passing a
device node through does not call for.

The node is mounted as well because a container cannot count on reaching `/sys`,
runtimes filtering it, and because a variable can be cleared or overridden by an
image's entrypoint:

```json
          {
            "hostPath": "/sys/firmware/devicetree/base/model",
            "containerPath": "/run/device-model",
            "options": [
              "rbind",
              "ro"
            ]
          }
```

The mount is read-only — nothing in the container has any business writing a
board name back — and a recursive bind, so it resolves the sysfs node itself
rather than whatever mount point it sits under.

The mount names the node and never the string it holds, so what a container
reads through it is whatever board it is running on. The variable does carry the
string, read when the file was written, which is as current as the file itself:
`qualcomm-cdi-generator.service` regenerates every spec from scratch on each
boot, so the name cannot outlive the board it was read from.

A host with no model node, or one whose node cannot be read, gets no variable
and no mount — the `fastrpc` specs are still written, since the DSP nodes and
their firmware do not depend on the board name, and an empty `MACHINE_NAME`
would be worse than none for a client resolving firmware paths against it.

### Group access

A generated `deviceNodes` entry is the path, the node's group and the mode to
create it with:

```json
          {
            "path": "/dev/dma_heap/system",
            "gid": 992,
            "fileMode": 432
          }
```

The `gid` is the group that owns the node on this board, so the node the
runtime creates inside the container belongs to the group the container is
being put into. It is named per node, because one class can span several groups
(see [Group access](#group-access)).

`fileMode` is **432, which is octal 0660** — `rw-rw----`. JSON has no octal
literal and the runtimes parse these files as strict JSON, so the mode is
written as the decimal number a mode really is; `0660` in the file would not
parse at all. The group bits are the point: the container user opens the node
as a member of the group named next to it, so a mode without them would hand
the container a node it still could not open. Nothing is granted to others.

Both fields describe the node the runtime creates **inside the container**, not
the host node they were resolved from. The host's nodes keep their own
ownership and mode — the generator reads the group and changes nothing. The
type and major/minor the runtime still fills in from the host node, and no
`uid` is named: the group is what access is expressed through, and naming an
owner would hand the node to a user the spec has no business choosing.

What the spec does carry is `containerEdits.additionalGids`: the groups owning
the host paths the class passes through, added to the container's supplementary
groups and nothing else — no group is picked for the container, which is also
what makes `--gid` a replacement rather than an addition. That is what makes a
group-accessible resource reachable —
a sound server socket bind-mounted with the host's permissions on it, or a
device node the runtime bind-mounts rather than re-creates — without relaxing a
mode on either side.

Access is expressed entirely within the CDI spec: the generated files carry no
`hooks`, so nothing of the generator's runs on the host at container start, and
the permissions of the host device nodes are never changed.

The group is read from the host path, because that is the group the driver and
its udev rules actually hand the device to, and it varies per board — a render
node is `render` on one target and the container user's primary group on
another. Reading it keeps the spec matched to the hardware it was generated on
instead of a compiled-in guess. The generator only reads the group; it does not
change it.

The group is resolved **per path**, not per class, because one class can span
several groups on a single board — a target may expose `/dev/video32` in `video`
while a node alongside it belongs to a vendor group:

```
INFO: Groups for 'v4l2' nodes: 44 (30 node(s)), 1002 (2 node(s))
```

The spec then grants every one of them:

```json
  "containerEdits": {
    "additionalGids": [ 44, 1002 ]
  }
```

Granting only the most common group would leave exactly the paths that differ
unreachable inside the container. Granting anything *beyond* them is not done
either — a spec lists what owns the paths it hands over and no `gid` chosen for
the container:

| Class | Groups on the host paths | `additionalGids` |
|-------|--------------------------|------------------|
| `gpu` | `renderD128` 993, `card0` 44 | `[ 44, 993 ]` |
| `v4l2` | every `/dev/video*` 44 | `[ 44 ]` |
| `dmaheap` | `/dev/dma_heap/system` 1000 | `[ 1000 ]` |
| `audio` | socket 29 | `[ 29 ]` |

A container whose user needs one of those groups as a supplementary group gets
it from the spec; one that needs some other group is given it on the `docker
run` command line, which is where a choice about the container's own identity
belongs.

##### When there is no group to read

**There is no fallback.** The group read off the host path is the only thing a
`gid` comes from, so a path that has none to read names none:

| The path | What the spec says |
|----------|--------------------|
| could not be `stat()`ed — it vanished between the glob and the `stat` | no `gid` on the entry, nothing granted for it |
| is owned by group `root` | no `gid` on the entry, nothing granted for it |
| is owned by any other group | that group, on the entry and in `additionalGids` |

A `root:root` node is passed over because it has no group to join in the first
place, and the runtime drops a `0` entry from `additionalGids`, so honouring it
would grant an unprivileged container nothing.

Nothing is substituted in either case — no group name looked up for the class,
no number compiled into the generator. A spec names the groups owning the paths
it hands over and no group picked for the container, and no number would be the
right one to pick anyway: `1000` is the container user's primary group under
qualcomm-linux (user `weston`) but not under every image, Debian/Ubuntu having
user `ubuntu` at uid 1000 with primary gid 1001. The nodes a working target
exposes carry a group of their own, which is the number that is actually right
for the board, and it is logged under `-vv` when there is none:

```
DEBUG: Cannot stat /dev/video0 for its group, naming none: [Errno 2] No such file or directory
DEBUG: /dev/dma_heap/system is owned by group root, naming no group for it
```

> **Note:** a node the host leaves `root:root` is therefore reachable only by a
> container running as root. There is no group to grant for it, and the node the
> runtime creates inside the container is `rw-rw----` with no `gid` named (see
> [Group access](#group-access)). Pass `--gid` if such a node has to be reached
> by an unprivileged container user.

For the socket-backed classes the group is read from the socket rather than a
device node; everything else is identical.

Override the resolution for all classes at once:

```shell
# qualcomm-cdi-generator.py --gid 1000
```

`--gid` replaces the group read from the host paths for every path and class,
so the spec grants exactly that one group, and it is what names a group on a
path that has none of its own.

### Example session

Captured on a board with no `gdsp` nodes, so there is no `fastrpc-gdsp` file. A
run also logs the `DSP_LIBRARY_PATH` it read and the directory each DSP class
resolved its firmware from, after the configuration directory below — see
[Which board's firmware](#which-boards-firmware).

```shell
(cdi) root@ventunoq:~# qualcomm-cdi-generator.py -v
INFO: Starting Qualcomm CDI generation
INFO: Config: destdir=/, cdifilename=qualcomm.json, dry_run=False
INFO: Groups to grant: from the host device nodes and sockets
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
INFO: Mounting the devicetree model /sys/firmware/devicetree/base/model into the container at /run/device-model
INFO: Found DSP configuration directory: /usr/share/qcom/conf.d
INFO: Wrote CDI JSON: /run/cdi/qualcomm-gpu.json
INFO: Wrote CDI JSON: /run/cdi/linux-v4l2.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-camera.json
INFO: Wrote CDI JSON: /run/cdi/linux-audio.json
INFO: Wrote CDI JSON: /run/cdi/linux-display.json
INFO: Wrote CDI JSON: /run/cdi/qualcomm-dmaheap.json
INFO: Setting MACHINE_NAME=Arduino Monza in the 'fastrpc-cdsp' spec
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-cdsp.json
INFO: Setting MACHINE_NAME=Arduino Monza in the 'fastrpc-adsp' spec
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-adsp.json
INFO: Completed Qualcomm CDI generation
(cdi) root@ventunoq:~# docker info
[...]
Server:
 CDI spec directories:
  /etc/cdi
  /var/run/cdi
 Discovered Devices:
  cdi: linux.org/audio=audio
  cdi: linux.org/audio=audio:all
  cdi: linux.org/display=display
  cdi: linux.org/display=display:all
  cdi: linux.org/v4l2=video0
  cdi: linux.org/v4l2=video1
  cdi: linux.org/v4l2=video10
  cdi: linux.org/v4l2=video11
  cdi: linux.org/v4l2=video12
  cdi: linux.org/v4l2=video13
  cdi: linux.org/v4l2=video14
  cdi: linux.org/v4l2=video15
  cdi: linux.org/v4l2=video16
  cdi: linux.org/v4l2=video17
  cdi: linux.org/v4l2=video18
  cdi: linux.org/v4l2=video19
  cdi: linux.org/v4l2=video2
  cdi: linux.org/v4l2=video20
  cdi: linux.org/v4l2=video21
  cdi: linux.org/v4l2=video22
  cdi: linux.org/v4l2=video23
  cdi: linux.org/v4l2=video24
  cdi: linux.org/v4l2=video25
  cdi: linux.org/v4l2=video26
  cdi: linux.org/v4l2=video27
  cdi: linux.org/v4l2=video28
  cdi: linux.org/v4l2=video29
  cdi: linux.org/v4l2=video3
  cdi: linux.org/v4l2=video4
  cdi: linux.org/v4l2=video5
  cdi: linux.org/v4l2=video6
  cdi: linux.org/v4l2=video7
  cdi: linux.org/v4l2=video8
  cdi: linux.org/v4l2=video9
  cdi: linux.org/v4l2=video:all
  cdi: qualcomm.com/camera=camera
  cdi: qualcomm.com/camera=camera:all
  cdi: qualcomm.com/dmaheap=dmaheap-system
  cdi: qualcomm.com/dmaheap=dmaheap-system:all
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp
  cdi: qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  cdi: qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  cdi: qualcomm.com/gpu=card0
  cdi: qualcomm.com/gpu=gpu:all
  cdi: qualcomm.com/gpu=kgsl-3d0
  cdi: qualcomm.com/gpu=render:all
  cdi: qualcomm.com/gpu=renderD128
[...]
(cdi) root@ventunoq:~# cdi classes
CDI device classes found:
  0. audio (1 vendors: linux.org)
  1. camera (1 vendors: qualcomm.com)
  2. display (1 vendors: linux.org)
  3. dmaheap (1 vendors: qualcomm.com)
  4. fastrpc-adsp (1 vendors: qualcomm.com)
  5. fastrpc-cdsp (1 vendors: qualcomm.com)
  6. gpu (1 vendors: qualcomm.com)
  7. v4l2 (1 vendors: linux.org)
(cdi) root@ventunoq:~# cdi devices
CDI devices found:
  0. linux.org/audio=audio
  1. linux.org/audio=audio:all
  2. linux.org/display=display
  3. linux.org/display=display:all
  4. linux.org/v4l2=video0
  5. linux.org/v4l2=video1
  6. linux.org/v4l2=video10
  7. linux.org/v4l2=video11
  8. linux.org/v4l2=video12
  9. linux.org/v4l2=video13
  10. linux.org/v4l2=video14
  11. linux.org/v4l2=video15
  12. linux.org/v4l2=video16
  13. linux.org/v4l2=video17
  14. linux.org/v4l2=video18
  15. linux.org/v4l2=video19
  16. linux.org/v4l2=video2
  17. linux.org/v4l2=video20
  18. linux.org/v4l2=video21
  19. linux.org/v4l2=video22
  20. linux.org/v4l2=video23
  21. linux.org/v4l2=video24
  22. linux.org/v4l2=video25
  23. linux.org/v4l2=video26
  24. linux.org/v4l2=video27
  25. linux.org/v4l2=video28
  26. linux.org/v4l2=video29
  27. linux.org/v4l2=video3
  28. linux.org/v4l2=video4
  29. linux.org/v4l2=video5
  30. linux.org/v4l2=video6
  31. linux.org/v4l2=video7
  32. linux.org/v4l2=video8
  33. linux.org/v4l2=video9
  34. linux.org/v4l2=video:all
  35. qualcomm.com/camera=camera
  36. qualcomm.com/camera=camera:all
  37. qualcomm.com/dmaheap=dmaheap-system
  38. qualcomm.com/dmaheap=dmaheap-system:all
  39. qualcomm.com/fastrpc-adsp=fastrpc-adsp
  40. qualcomm.com/fastrpc-adsp=fastrpc-adsp:all
  41. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp
  42. qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all
  43. qualcomm.com/gpu=card0
  44. qualcomm.com/gpu=gpu:all
  45. qualcomm.com/gpu=kgsl-3d0
  46. qualcomm.com/gpu=render:all
  47. qualcomm.com/gpu=renderD128
```

You can then pass one or more of the above entries to the runtime:
```shell
(cdi) root@ventunoq:~# docker run --network host --device qualcomm.com/gpu=renderD128 --device=qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all --rm -it aiml-container:latest /bin/bash
```
When using a `fastrpc` CDI device, the container also gets the board name the
FastRPC library looks for, as a variable and as the mounted devicetree node
(see [Board name](#board-name)):
```shell
(cdi) root@ventunoq:~# docker run --network host --device=qualcomm.com/fastrpc-cdsp=fastrpc-cdsp:all --rm -it aiml-container:latest /bin/bash
root@container:/# echo $MACHINE_NAME
Arduino Monza
root@container:/# cat /run/device-model
Arduino Monza
```

To generate only the fastrpc classes:
```shell
(cdi) root@ventunoq:~# qualcomm-cdi-generator.py --classes fastrpc-cdsp,fastrpc-adsp -v
INFO: Starting Qualcomm CDI generation
INFO: Config: destdir=/, cdifilename=qualcomm.json, dry_run=False
INFO: Groups to grant: from the host device nodes and sockets
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
INFO: Mounting the devicetree model /sys/firmware/devicetree/base/model into the container at /run/device-model
INFO: Found DSP configuration directory: /usr/share/qcom/conf.d
INFO: Setting MACHINE_NAME=Arduino Monza in the 'fastrpc-cdsp' spec
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-cdsp.json
INFO: Setting MACHINE_NAME=Arduino Monza in the 'fastrpc-adsp' spec
INFO: Wrote CDI JSON: /run/cdi/qualcomm-fastrpc-adsp.json
INFO: Completed Qualcomm CDI generation
```

Note that every class is still probed; `--classes` selects which CDI files are
written, not what the generator looks at.

### CDI file structure

The tool writes one JSON file per device class (see [Device
classes](#device-classes)). Each file has a `kind` of `<vendor>/<class>` and is named after that vendor -- `qualcomm-gpu.json` holds `qualcomm.com/gpu` and `linux-audio.json` holds `linux.org/audio` -- so a kind is readable off the file holding it. A `deviceNodes` entry is the node's path, its owning group as `gid` and a `fileMode` of 432 (octal 0660), and the spec-level `containerEdits` sets `additionalGids` to every group owning one of the class's host paths (see [Group access](#group-access)). The `fastrpc` files additionally include bind-mounts for the individual Hexagon firmware files their own DSP's FastRPC client opens, presented under `/usr/share/hexagon-dsp/` and taken from the board directory the hexagon-dsp configuration names, along with that configuration directory and no other directory of the firmware tree (see [DSP firmware mounts](#dsp-firmware-mounts)), since those binaries are tightly coupled to the in-kernel firmware loader, and the devicetree model node holding the board name (see [Board name](#board-name)). The `fastrpc` files are also the only ones carrying an `env` entry, `MACHINE_NAME`, which is the only variable any generated file sets (see [The container environment](#the-container-environment)).

The specs declare `cdiVersion` `0.7.0`, the first CDI version that defines
`additionalGids`; see [Minimum runtime versions](#minimum-runtime-versions) for
the runtime releases that accept it.

##### The generated-file note

JSON has no comment syntax, so the "this file was generated" header rides in the
spec's own `annotations` map, emitted between `kind` and `devices` where the
comment would have gone:

```json
  "annotations": {
    "qualcomm.com/generated-by": "qualcomm-cdi-generator",
    "qualcomm.com/generated-note": "This file is generated from the devices discovered on this host. ..."
  },
```

The keys are qualified with the vendor so they read like the `kind` they sit next
to and cannot collide with another tool's metadata on the same spec. CDI defines
`annotations` as specific to itself and not affecting container metadata, so the
note describes the file to whoever reads it and never reaches the workload. It is
omitted from the examples below for brevity; every generated file carries it.

Only fields the CDI `Spec` struct declares may be emitted. The runtimes
unmarshal these files with unknown fields disallowed and reject the whole spec on
the first key they do not recognise, which costs the container every device in
that class. The CDI JSON schema sets no `additionalProperties: false`, so neither
it nor `cdi validate` catches a stray field; the test suite checks the emitted
field names against the struct instead.

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
          { "path": "/dev/dri/renderD128", "gid": 110, "fileMode": 432 }
        ]
      }
    },
    {
      "name": "render:all",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/renderD128", "gid": 110, "fileMode": 432 }
        ]
      }
    },
    {
      "name": "card0",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/card0", "gid": 44, "fileMode": 432 }
        ]
      }
    },
    {
      "name": "gpu:all",
      "containerEdits": {
        "deviceNodes": [
          { "path": "/dev/dri/card0", "gid": 44, "fileMode": 432 },
          { "path": "/dev/dri/renderD128", "gid": 110, "fileMode": 432 }
        ]
      }
    }
  ],
  "containerEdits": {
    "additionalGids": [ 44, 110 ]
  }
}
```

The `44` and `110` are the groups owning the card and render nodes on this
board, named on each node entry as well as granted here, and they are all the
spec grants.

A `/dev/kgsl-3d0` node, where present, appears alongside `card0` under its own
`kgsl-3d0` name and in `gpu:all`.

Example `linux-audio.json`, where the socket mount rides on the device entry
so that requesting the device is what mounts the socket. This host runs its
audio session under uid 1001, so `hostPath` keeps that uid while
`containerPath` is the system-wide location the container resolves (see [Where
the sockets are mounted](#where-the-sockets-are-mounted)):
```json
{
  "cdiVersion": "0.7.0",
  "kind": "linux.org/audio",
  "devices": [
    {
      "name": "audio",
      "containerEdits": {
        "mounts": [
          {
            "hostPath": "/run/user/1001/pulse/native",
            "containerPath": "/run/pulse/native",
            "options": [ "nosuid", "noexec", "rw", "bind" ]
          }
        ]
      }
    }
  ],
  "containerEdits": {
    "additionalGids": [ 1001 ]
  }
}
```

Note that `additionalGids` follows the **host** socket's owning group (1001
here): the container has to join the group that owns the socket it is actually
connecting to, whatever path it reaches it by.

The `audio:all` entry is omitted above for brevity; with one socket it carries
the same mount, so both spellings behave alike. A host running two sound
servers names them `audio0` and `audio1` instead, each carrying its own socket,
and `audio:all` then carries both (see [Every login's displays and sound
servers](#every-logins-displays-and-sound-servers)).

`linux-display.json` has the same shape, with the compositor's socket in
place of the sound server's. Its spec-level `containerEdits` carries the group
and nothing else — the compositor is reached through the mounted socket, and
`MACHINE_NAME` is set by the `fastrpc` specs alone (see [The container
environment](#the-container-environment)):

```json
  "containerEdits": {
    "additionalGids": [ 1000 ]
  }
```

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
It needs PyYAML, which the generator itself imports (see
[Requirements](#requirements)); the validation tests additionally need `cdi`,
located via the `CDI_TOOL` environment variable or `PATH` and skipped when it
is not found.

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
