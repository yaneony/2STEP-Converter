# 2STEP-Converter

Converts **STL, 3MF, OBJ, AMF, and IGES** files to STEP using OpenCASCADE. Includes automatic repair, optional mesh reduction, and PNG previews.

Current version: **4.0.0**

![2STEP-Converter terminal UI showing batch conversion progress with the read, sew, fix, refine, write, and preview steps for each file](docs/converter.png?v=20260731)

![Version](https://img.shields.io/badge/version-4.0.0-purple?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.12-blue?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)

![Stars](https://img.shields.io/github/stars/yaneony/2STEP-Converter?style=for-the-badge)
![Issues](https://img.shields.io/github/issues/yaneony/2STEP-Converter?style=for-the-badge)
![Last Commit](https://img.shields.io/github/last-commit/yaneony/2STEP-Converter?style=for-the-badge)

---

Put your files in `models/` and run the launcher. The converter builds CAD solids, merges co-planar faces, and attempts repair when it detects mesh damage. If an input cannot pass the enabled geometry checks, it reports a failure and continues with the next file.

> [!TIP]
> **No system Python required.** The launcher installs its own environment using `src/environment.yml`. Its PATH changes apply only to the launcher process. Installation normally needs no administrator access; enabling Windows long paths is an optional exception.

![Side-by-side comparison: typical online converter on the left showing thousands of triangle faces, vs 2STEP-Converter on the right showing a clean solid - same source file](docs/compare.png)

*Left: typical online converter. Right: 2STEP-Converter. Same source file.*

![Four auto-generated PNG previews of the same model at 20%, 40%, 60%, and 80% reduction, output by the converter itself](docs/result.png)

*Auto-generated `.png` previews showing the same model at 20%, 40%, 60%, and 80% reduction.*

---

## Table of Contents

- [What to Expect](#what-to-expect)
- [Installation](#installation)
- [Usage](#usage)
- [Model Repair](#model-repair)
- [First Run](#first-run)
- [How It Works](#how-it-works)
- [Geometry Fidelity](#geometry-fidelity)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Requirements](#requirements)
- [Project Structure](#project-structure)
- [Credits](#credits)
- [Disclaimer](#disclaimer)
- [Authorship and AI Assistance](#authorship-and-ai-assistance)
- [License](#license)

---

## What to Expect

The output is a B-Rep: CAD geometry made from faces, edges, shells, and solids. Flat triangle regions can become larger planar faces. Recognized primitives and suitable holes can become exact CAD surfaces. Other curved regions keep their mesh facets.

You can use the resulting solids for CAD operations, but conversion does not recreate the original sketches, constraints, dimensions, or feature history.

| Source geometry | Expected result |
|-----------------|-----------------|
| Clean, closed, consistently oriented mesh | One or more STEP solids, subject to the enabled geometry checks. Reduction and analytic fitting can change the tessellated shape within their configured limits. |
| Large co-planar triangle regions | Usually merged into fewer planar CAD faces. |
| Confidently recognized complete spheres, cylinders, and cones | Replaced by exact analytic CAD surfaces. |
| Complete linear extrusion with matching planar end profiles | Rebuilt as an exact profile prism when experimental parametric reconstruction is enabled. |
| Verified straight through-holes and flat-bottomed blind holes | Replaced by analytic cylindrical cuts. |
| Freeform curves, threads, fillets, text, organic shapes, and ambiguous holes | Preserved as faceted B-Rep geometry. They can still belong to a real solid. |
| Several disconnected closed mesh parts | Exported as several solids in one STEP file. |
| Open, non-manifold, self-intersecting, or badly damaged mesh | Automatic repair is attempted where applicable. Unresolved or ambiguous damage causes conversion to fail. |
| IGES geometry | Imported into OpenCASCADE and processed through the CAD conversion path. Mesh reduction does not apply. |

By default, the converter requires valid solids and checks the STEP file again after writing it. These checks detect many topology and geometry problems. They cannot certify every dimension or determine what a damaged model was intended to look like.

Before using an output for manufacturing or other critical work:

1. Use `--reduce 0` when small details matter more than file size.
2. Open the STEP file in CAD and check the solid count and physical size.
3. Measure important dimensions, thin walls, and holes. Inspect any repaired areas.

STEP export uses millimeters. STL and OBJ numeric coordinates are used as supplied because their units cannot be inferred reliably. 3MF and AMF units and instance transforms are applied during loading. Verify the imported physical size in CAD.

The 3MF reader also handles missing Production Extension declarations for `p:path` and `p:UUID` in some Creality projects. This correction happens in memory; it does not rewrite the source archive.

---

## Installation

Download and extract the project, then let the launcher install its environment.

1. **Download** the project:
   - Choose a version from [GitHub Releases](https://github.com/yaneony/2STEP-Converter/releases), or use **Code > Download ZIP** on the [repository page](https://github.com/yaneony/2STEP-Converter) for the current development code.
   - Or clone with git: `git clone https://github.com/yaneony/2STEP-Converter.git`

2. **Extract** the archive to a folder of your choice. Keep the path short on Windows (e.g. `C:\Tools\2STEP-Converter`) to avoid the 260-character path limit - see [Windows 260-character path limit](#windows-260-character-path-limit).

3. **Put your input files in `models/`.** Create the folder if it is absent. Only files directly inside it are scanned, not subfolders.

4. **Run the launcher:**
   - **Windows** - double-click `2STEP-Converter.bat`
   - **macOS / Linux** - make executable once with `chmod +x 2STEP-Converter.sh`, then `./2STEP-Converter.sh`

5. **Choose the environment location** if asked: portable `lib/` beside the launcher, or the platform default under your user profile. The first setup downloads Python and the dependencies and needs several GB of disk space. Allow extra time for downloading and unpacking. See [First Run](#first-run).

6. **Choose reduction after the update check.** Enter `0` to keep all triangles, enter a percentage for the whole batch, or press Enter to choose separately for each clean mesh. Conversion then starts. STEP files and enabled previews appear beside the inputs.

> [!TIP]
> Paths containing spaces must be quoted in command-line arguments, for example `2STEP-Converter.bat "models\my part.stl"`.

---

## Usage

### Batch mode

1. Drop files into the `models/` folder
2. Run the launcher:
   - **Windows** - double-click `2STEP-Converter.bat`
   - **macOS / Linux** - `./2STEP-Converter.sh` (one-time `chmod +x 2STEP-Converter.sh`)
3. Wait for the update check, choose reduction, and let conversion finish.
4. Output `.stp` files and enabled `.png` previews appear in the same folder.

**Supported formats:** `.stl` `.3mf` `.obj` `.amf` `.igs` `.iges`

### Single file

**Windows**
```bat
2STEP-Converter.bat model.stl
2STEP-Converter.bat model.stl -o out.stp
```

**macOS / Linux**
```sh
./2STEP-Converter.sh model.stl
./2STEP-Converter.sh model.stl -o out.stp
```

### Multiple files

Pass any number of files directly - no need to use the `models/` folder:

**Windows**
```bat
2STEP-Converter.bat a.stl b.obj c.3mf
```

**macOS / Linux**
```sh
./2STEP-Converter.sh a.stl b.obj c.3mf
```

### Options

| Option | Default | Description |
|--------|:-------:|-------------|
| `--tolerance` / `-t` | from config (`0.01`) | Maximum sewing distance in model units. Scale-aware sewing can use a smaller value for mesh inputs. |
| `--reduce` / `-r` | prompt or automatic | Percentage of triangles to remove: `25` keeps about 75%, `0` disables reduction. `25,50,75` requests three outputs. |
| `--output` / `-o` | - | Exact output path for one input and one reduction variant. |
| `--output-dir` / `-d` | - | Write all outputs to this directory instead of alongside the source. Later inputs that target an already claimed output name are skipped. |
| `--format` | from config (`ap203`) | STEP schema: `ap203`, `ap214`, or `ap242`. |
| `--force` / `-f` | off | Convert again even when an existing output is up to date. |
| `--dry-run` / `--dry` | off | List planned conversions and skips without writing STEP files or previews. Normal startup and input inspection still apply. |
| `--watch` / `-w` | off | Watch the configured input folder (`models/` by default) after the initial batch. New, changed, and previously failed files are retried after their size and timestamp stabilize. Ctrl+C to stop. |
| `--preview` / `--no-preview` | from config | Force the `.png` preview on or off (overrides `GENERATE_PNG_PREVIEW`). |
| `--experimental-parametric` / `--no-experimental-parametric` | from config | Enable or disable experimental exact linear-extrusion reconstruction. The stable fallback remains active. |
| `--repair` / `--no-repair` | from config (on) | Enable or disable automatic volume repair for detected mesh damage. |
| `--update-check` / `--no-update-check` | on | Enable or disable the GitHub release check. This does not install updates. |
| `--pause` / `--no-pause` | automatic | Force or suppress the final 'Press Enter' prompt. By default it appears only in an interactive terminal. |
| `--version` | - | Print the application version and exit. |
| `--help` / `-h` | - | Show available arguments and exit. |

Reduction values must be at least `0` and less than `100`. Decimal values are rounded to nine decimal places for parsing and output names.

`--output` requires exactly one input and one reduction variant, and cannot be combined with `--output-dir`. `--watch` works only on the configured input folder; it cannot be combined with explicit input files or `--dry-run`.

Dry-run can still check for updates, inspect meshes, and create a missing configuration or input folder. When invoked through a launcher, environment setup and dependency checks also happen first.

**Windows**
```bat
2STEP-Converter.bat --reduce 25 model.stl
2STEP-Converter.bat --reduce 25,50,75 model.stl
2STEP-Converter.bat --format ap214 -d C:\out model.stl
2STEP-Converter.bat --experimental-parametric model.stl
2STEP-Converter.bat --no-preview --dry-run
2STEP-Converter.bat --watch
```

**macOS / Linux**
```sh
./2STEP-Converter.sh --reduce 25 model.stl
./2STEP-Converter.sh --reduce 25,50,75 model.stl
./2STEP-Converter.sh --format ap214 -d ~/out model.stl
./2STEP-Converter.sh --experimental-parametric model.stl
./2STEP-Converter.sh --no-preview --dry-run
./2STEP-Converter.sh --watch
```

### Interactive reduction prompt

When `ASK_FOR_REDUCTION` is enabled (default), an interactive prompt appears after the update-check result, before loading models. Enter a percentage for all meshes that do not need volume repair, or press Enter to choose separately for each mesh. `0` keeps all triangles. Comma-separated percentages request multiple outputs; a custom `--output` path requires one percentage.

Passing `--reduce`, using `--dry-run`, disabling `ASK_FOR_REDUCTION`, or redirecting input skips interactive reduction questions. An explicit list containing only IGES inputs does not ask for reduction. When the update check is disabled, the common prompt follows the startup banner. Invalid startup percentages are reported and requested again.

In per-model mode, Enter accepts the configured or automatic default shown for that mesh. Inputs requiring volume repair skip this prompt and are processed without reduction. Watch mode keeps the common choice for new files; if you chose per-model questions, it asks for new clean meshes too.

Common startup choice:

| Input | Result |
|-------|--------|
| **Enter** | Ask separately for each clean model. |
| `25` | Reduce every clean model by 25 percent. |
| `25,50,75` | Produce these three reduction variants for every clean model. |
| `0` | Disable reduction for all models in this run. |

Per-model choice after an empty startup answer:

| Input | Result |
|-------|--------|
| **Enter** | Accept this model's configured or automatic default. |
| `25` | Reduce this file by 25% |
| `25,50,75` | Generate three outputs at 25%, 50%, and 75% reduction |
| `!25` or `!25,50` | Lock the value for the remaining batch and future watch inputs. |
| `0` | No reduction for this file |

### Automatic reduction

With the default settings, meshes above 50,000 triangles are automatically reduced toward that target when no explicit percentage or nonzero configured default was selected. Smaller meshes keep all triangles. Geometry checks can reject a reduction even when the requested count is achievable.

The numeric default `DEFAULT_REDUCTION_PERCENT: 0` leaves automatic reduction available. Entering `0` at a prompt or passing `--reduce 0` explicitly disables it for the chosen inputs. To disable it permanently, set `AUTO_REDUCTION_ENABLED` to `false` and leave `DEFAULT_REDUCTION_PERCENT` at numeric `0`.

### Output names and existing files

| Conversion | Default output name |
|------------|---------------------|
| No reduction | `model [0].stp` |
| 25 percent reduction | `model [25].stp` |
| Detected damage requiring volume repair | `model [repaired] [0].stp` |

An enabled preview uses the same name with `.png`. Preview errors are reported separately; a successful STEP export can remain available even if the preview fails. `--output` overrides the generated path, and `--output-dir` changes the destination folder. No conversion JSON sidecar is created.

Different input formats with the same stem can target the same output. In a batch, the first successful or up-to-date source claims that path; later conflicting inputs are skipped. Watch mode keeps this ownership while the source remains in the folder. Use distinct source names or separate output locations when you need both conversions.

An existing STEP file is skipped only if it is nonempty and not older than the source, converter modules, environment specification, or configuration. A required preview must also be nonempty and not older than the STEP file. Explicit tolerance, format, repair, or experimental reconstruction switches bypass this cache, as does `--force`.

Use `--force` when changing conversion choices while reusing a custom `--output` path, because that filename does not encode the reduction percentage.

### Update notifications

At startup the converter prints `Checking for updates...` and checks the latest stable [GitHub release](https://github.com/yaneony/2STEP-Converter/releases). When a newer version is available, it shows the installed version, the new version and a download link. Otherwise it confirms that the installed version is up to date. If GitHub cannot be checked, it reports that separately and continues conversion. The check has a two-second waiting limit. Updates are downloaded and installed manually.

Use `--no-update-check` to skip the request. `--help`, `--version` and Python library conversions do not check for updates. The installed version is defined once in `src/version.py`; update `VERSION` when preparing a release and use the matching GitHub tag, such as `v4.0.0`.

---

## Model Repair

Automatic volume repair is enabled by default for detected damage in closed meshes. It is experimental and cannot repair every input. You can explicitly enable it for a run:

```bat
2STEP-Converter.bat --repair "models\model.stl"
```

On macOS or Linux, pass the same options to `./2STEP-Converter.sh`. `REPAIR_INTERSECTING_MESH` defaults to `true`, including when the key is absent from an existing configuration. Set it to `false` in `data/config.json` to disable automatic repair permanently, or use `--no-repair` for one run.

Repair inspects mesh quality even when ordinary quality checks are disabled. Internal intersections, non-manifold edges or vertices, or non-orientable topology can trigger an OpenCASCADE volume-building attempt in an isolated worker.

- Small attached triangles or surface patches can be removed when the correction is unambiguous and retains every source vertex. `MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO` limits removed area to `0.005` (0.5 percent) by default. Set it to `0` to disable this correction.
- Material and cavities are classified using an even-odd surface-crossing rule. This is an interpretation of damaged geometry; it cannot recover the designer's intent or reliably combine arbitrary overlapping surfaces. Cross-component overlaps alone do not trigger repair.
- Open boundaries, ambiguous material classification, invalid solids, or outer-bound changes beyond the allowed tolerance cause repair to fail. Repaired outputs must pass strict solid validation and STEP readback even if ordinary validation settings were disabled.
- Original files are never rewritten. Failed repair preserves an existing STEP output. Inspect repaired walls, cavities and body connections in CAD before use.

Clean inputs retain their normal reduction controls. A mesh requiring volume repair skips reduction and produces one output marked `[repaired]`, even when several reduction percentages were requested. Batch reduction choices continue to apply to later clean inputs.

The configured intersection scan limit still applies. Large inputs without detected damage follow the ordinary pipeline when the scan is skipped; this does not prove they have no intersections. If other detected damage requires volume repair and the intersection scan was skipped, repair fails with a request to increase `SELF_INTERSECTION_CHECK_MAX_TRIANGLES`. Use `0` to remove the limit. Volume repair applies only to mesh formats; IGES inputs automatically follow the ordinary CAD conversion pipeline.

The console reports solid count, repaired volume, outer-bound changes and a comparison with the damaged source's signed volume. That source volume is only a diagnostic reference, not a reliable measure of the original material.

Python callers receive repair measurements in `result['repair']`, or `None` when repair was unnecessary. These include `removed_attached_triangles`, `removed_attached_area` and `removed_attached_area_ratio`. `result['reduction_fraction']` reports the actual reduction choice, and `result['reduction_skipped']` indicates a skipped reduction. Library calls keep the supplied output path, and repair is enabled in `Settings()` by default.

---

## First Run

The launcher prepares the environment before starting the converter:

1. Selects an installation folder.
2. Downloads the pinned micromamba executable if missing and verifies its SHA-256 checksum.
3. Creates the Python environment from `src/environment.yml` if needed.
4. Updates dependencies when that specification changes. If required imports fail, it attempts a forced reinstall and checks them again.
5. Starts `src/converter.py` with your command-line arguments.

The environment includes Python 3.12, pythonocc-core, NumPy, Open3D, trimesh, fast-simplification, NetworkX, matplotlib, and Pillow. Direct versions are pinned in the specification; it is not a complete lockfile for every transitive dependency and platform build.

Download size, setup time, and disk use vary by platform and resolved packages. Expect several GB for the environment and package cache, and leave room for large models and STEP outputs. Normal conversion uses the local environment. Internet access is needed for setup, dependency updates or repairs, and the optional release check.

### Install location

The launcher chooses an existing directory in this order:

1. `lib/` beside the launcher.
2. The platform default below.
3. If neither exists, it asks where to install.

An existing directory is selected even if its environment still needs setup or repair.

| Platform | Default path |
|----------|--------------|
| Windows | `%LOCALAPPDATA%\2STEP-Converter` |
| macOS | `~/Library/Application Support/2STEP-Converter` |
| Linux | `$XDG_DATA_HOME/2STEP-Converter`, or `~/.local/share/2STEP-Converter` if unset |

Python lives in `env/` under the selected directory. Micromamba and its package cache also live there. Portable mode keeps these files beside the project; the platform default keeps them in your user profile. Do not remove individual environment libraries to save space, as this can break imports or conversion.

### Windows 260-character path limit

If `LongPathsEnabled` is not enabled, the Windows launcher offers two choices before setup:

| Choice | What happens |
|--------|--------------|
| Enable long paths | Requests administrator access to change the registry, then exits. Restart Windows and run the launcher again. |
| Use `%LOCALAPPDATA%\2STEP-Converter` | Selects the user-profile location without changing the registry or requiring a restart. This overrides a previously selected portable location. |

Use a short project path, such as `C:\Tools\2STEP-Converter`. The user-profile option can shorten environment paths, but very long profile or output paths can still cause problems.

---

## How It Works

The main paths share the same STEP export stage, but not every input needs every operation.

| Stage | Mesh inputs: STL, 3MF, OBJ, AMF | IGES inputs |
|-------|-------------------------------|-------------|
| Read | Load vertices and triangles; apply 3MF/AMF units and instance transforms. OBJ polygons are triangulated. | Import B-Rep geometry with OpenCASCADE. |
| Inspect | Clean the mesh and check enabled quality diagnostics. Detected closed-mesh damage can trigger volume repair. | Continue to CAD processing. |
| Reduce and fit | For meshes that do not need volume repair, optionally reduce triangles and try recognized primitives or experimental extrusions. | Mesh reduction and mesh fitting are skipped. |
| Build CAD geometry | Use the repaired solid, a fitted shape, or shared triangle faces and edges. | Use the imported shape. |
| Process topology | Where needed, sew faces, fix topology, close small planar gaps, build solids, and merge co-planar faces. | Use the same CAD operations where applicable. |
| Fit holes | Try qualifying straight through-holes and flat-bottomed blind holes. This stage is skipped for volume-repaired meshes. | Skipped. |
| Export | Write a temporary STEP file, perform the enabled readback checks, and atomically replace the destination. | Same. |
| Preview | If enabled, render a PNG from the exported STEP file. | Same. |

Open3D supplies the primary mesh reducer. If its result fails, fast-simplification provides alternative attempts. Every accepted reduction must pass the configured geometry checks. Trimesh is used for optional small mesh hole filling, not as a reduction fallback.

Valid mesh topology can bypass sewing, and valid B-Rep geometry can bypass topology fixing. Recognized complete primitives also bypass the ordinary triangle conversion stages. Geometry stays in double precision during triangle B-Rep construction, without an intermediate STL export.

Sewing, fixing, gap filling, solid construction, refinement, volume repair, and hole reconstruction use isolated CAD workers with configured timeouts. Worker failures can be reported without terminating the main process. Other native operations, including STEP export, run in the main process, so isolation does not cover every possible kernel crash.

STEP replacement happens only after the enabled checks succeed. Detected source changes abort conversion rather than replacing an output with geometry from an outdated read. Original input files are not rewritten.

### Time estimates

The estimator learns from successful conversions and saves history in `data/estimator.json`. It separates measurements by input format, application version, and conversion settings, keeping up to 64 recent samples per history bucket.

After five matching measurements, it can show an approximate remaining time and range. It prefers history for the input format, then tries pooled-format history with the same settings. Older history may appear as a `legacy estimate`. The early estimate uses input triangle count; a later estimate uses the sewn face count and includes the remaining export and enabled preview work.

`err ~N%` describes errors on past predictions. `extrapolated` means the current size is outside the learned range. Neither is a promise for the current model: topology, repair work, and system load can change conversion time substantially. With insufficient history, the console shows learning progress instead.

---

## Geometry Fidelity

### What counts as a solid?

A faceted model can still be a valid CAD solid. Its curved areas may consist of many flat faces, but its closed boundary encloses material. Seeing faces in a CAD tree does not mean the output is surface-only geometry.

| Term | Meaning |
|------|---------|
| Face | A bounded part of a surface. |
| Shell | A connected collection of faces. It may be open or closed. |
| Solid | A closed boundary representing a volume, possibly with internal cavities. |
| Compound | A container that can hold several independent solids. |

By default, every output component must belong to a valid solid. Free shells, faces, wires, edges, or vertices cause rejection. The converter also imports the temporary STEP file again and compares the result before replacing the destination.

Geometry comparisons include per-solid bounds, volume, center of mass, and normalized inertia. These can detect changes that a bounding box or total volume alone would miss. Measurements use local coordinates for distant geometry to reduce numerical errors. They are useful checks, not proof that two arbitrary shapes are identical.

Nested closed shells are interpreted as cavities and material islands. Partially overlapping closed components remain separate bodies; the converter does not automatically fuse them into one union. If planar refinement or hole fitting fails the source comparison, the converter can retain the original triangle solids when they pass the checks.

Disabling `REQUIRE_SOLID_OUTPUT` permits valid surface geometry. Disabling `VALIDATE_STEP_AFTER_WRITING` removes the readback checks. Neither setting repairs a damaged input, and volume-repaired outputs always require strict solid validation and readback.

### Reduction and sewing tolerance

Reduction trades mesh detail for fewer triangles. The converter checks component count, component dimensions and position, enclosed volume, and sampled surface distances in both directions. With boundary preservation enabled, it also checks watertightness and each component's Euler characteristic, helping detect lost tunnels or other topology changes.

These limits can reject an aggressive reduction, but small features can still change within the accepted limits. The distance check samples vertices, triangle centers, and edge midpoints; it is not a certified maximum error over the entire surface. Use `--reduce 0` for small holes, thin walls, text, or detailed curves.

For mesh inputs, scale-aware sewing is enabled by default. Its effective distance is:

`min(requested tolerance, max(model diagonal * SCALE_AWARE_SEWING_TOLERANCE_RATIO, 1e-9))`

The requested tolerance comes from `--tolerance` or `SEWING_TOLERANCE`. Increasing it may have no effect while the scale-based value is smaller. IGES uses the requested tolerance directly. A larger tolerance can join unintended nearby edges, so change it only after inspecting the source.

### Intentional holes and accidental gaps

A properly modeled through-hole belongs to a closed mesh surface; it does not count as an open boundary. Missing triangles do create open boundaries.

- Mesh hole filling is off by default. `FILL_SMALL_MESH_HOLES` asks trimesh to fill simple small gaps, but can also close an intentional open boundary.
- Small planar B-Rep gap filling is on by default. Both `MAX_BREP_GAP_EDGE_COUNT` and `MAX_BREP_GAP_AREA_RATIO` must be satisfied.
- Large, irregular, or ambiguous openings can still prevent solid conversion and need repair in a mesh editor.

### Analytic primitives and holes

Complete spheres, capped cylinders, and cones can be rebuilt as exact CAD primitives when the mesh is watertight, has one connected component, and passes the configured surface-fit and volume limits. Fits check triangle interiors as well as vertices. Uneven tessellation is handled with local circle fitting rather than an average vertex center.

Coarse meshes may remain faceted because replacing their flat facets with a smooth surface would exceed the volume limit. Partial primitives and curved regions attached to complex geometry are not replaced by a full primitive. A failed fit keeps the triangle geometry.

Straight through-holes and flat-bottomed blind holes are also recognized conservatively. Their polygonal openings must fit circles, the surrounding material and empty interior must match a cylinder, and the Boolean cut must improve face count while passing topology and local and global volume checks.

The fitted cut sits just outside the original polygon vertices to remove the old facets, so its radius can be slightly larger than the faceted opening. Ambiguous, tapered, threaded, or intersecting holes stay faceted. An individual straight section of a stepped hole can be reconstructed only if it independently passes the checks.

Use `--reduce 0` to give fitting the original cleaned mesh instead of a simplified one. Keep the default limits unless you have measured the intended geometry.

### Experimental parametric reconstruction

This mode is off by default. Enable it with `--experimental-parametric` or `EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION` in the configuration.

It currently recognizes complete linear extrusions: two matching planar end profiles, with side faces following the same extrusion direction. Internal profile loops are retained. A candidate must form one valid solid, reduce face count, and stay within the fitting and volume limits. If it fails, conversion continues through the ordinary triangle B-Rep path.

The result is exact profile-prism geometry, not editable sketches or a feature tree. General sweeps, feature histories, and arbitrary freeform surface reconstruction are not implemented.

---

## Configuration

The CLI reads `data/config.json` and creates it on the first normal run if absent. Edit it with a text editor. Command-line overrides apply to that run without rewriting the configuration.

For everyday use, the main choices are reduction, preview, repair, and the input folder. The remaining settings control geometry checks and fitting. Keep the default validation limits unless you understand the source geometry.

Invalid values and unknown keys produce startup warnings. Invalid values fall back to defaults without rewriting your existing file. Input and output suffixes must be distinct, contain no path separators or extra dots, and reserve `.iges` for IGES. Conflicting suffix overrides fall back to the default extensions.

<details>
<summary>Complete default configuration</summary>

```json
{
    "SEWING_TOLERANCE": 0.01,
    "DEFAULT_REDUCTION_PERCENT": 0,
    "AUTO_REDUCTION_ENABLED": true,
    "AUTO_REDUCTION_TARGET_TRIANGLES": 50000,
    "ASK_FOR_REDUCTION": true,
    "SKIP_UP_TO_DATE_OUTPUTS": true,
    "PLANAR_MERGE_ANGLE_RADIANS": 0.01,
    "SEWING_TIMEOUT_SECONDS": 1800,
    "CAD_OPERATION_TIMEOUT_SECONDS": 300,
    "SEW_PARTS_SEPARATELY": true,
    "DEFAULT_STEP_FORMAT": "ap203",
    "GENERATE_PNG_PREVIEW": true,
    "INPUT_FOLDER_NAME": "models",
    "CHECK_MESH_QUALITY": true,
    "REPAIR_MESH_BEFORE_CONVERSION": true,
    "REPAIR_INTERSECTING_MESH": true,
    "MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO": 0.005,
    "VERTEX_MERGE_DISTANCE": 0.0,
    "FIX_TRIANGLE_ORIENTATION": true,
    "REMOVE_NON_MANIFOLD_TRIANGLES": false,
    "REJECT_NON_MANIFOLD_MESH": false,
    "FILL_SMALL_MESH_HOLES": false,
    "FILL_SMALL_PLANAR_BREP_GAPS": true,
    "MAX_BREP_GAP_EDGE_COUNT": 8,
    "MAX_BREP_GAP_AREA_RATIO": 0.005,
    "CHECK_SELF_INTERSECTIONS": true,
    "SELF_INTERSECTION_CHECK_MAX_TRIANGLES": 50000,
    "REJECT_SELF_INTERSECTING_MESH": false,
    "USE_SCALE_AWARE_SEWING_TOLERANCE": true,
    "SCALE_AWARE_SEWING_TOLERANCE_RATIO": 0.000001,
    "REQUIRE_SOLID_OUTPUT": true,
    "VALIDATE_STEP_AFTER_WRITING": true,
    "PRESERVE_BOUNDARIES_DURING_REDUCTION": true,
    "REDUCTION_BOUNDARY_WEIGHT": 10.0,
    "MAX_REDUCTION_SIZE_CHANGE_PERCENT": 0.5,
    "MAX_REDUCTION_VOLUME_CHANGE_PERCENT": 2.0,
    "MAX_REDUCTION_SURFACE_DEVIATION_PERCENT": 0.1,
    "EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION": false,
    "EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO": 0.0005,
    "EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT": 0.1,
    "RECONSTRUCT_ANALYTIC_PRIMITIVES": true,
    "ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO": 0.001,
    "ANALYTIC_PRIMITIVE_MIN_TRIANGLES": 32,
    "ANALYTIC_PRIMITIVE_MAX_VOLUME_CHANGE_PERCENT": 0.1,
    "RECONSTRUCT_ANALYTIC_THROUGH_HOLES": true,
    "RECONSTRUCT_ANALYTIC_BLIND_HOLES": true,
    "ANALYTIC_HOLE_FIT_ERROR_RATIO": 0.002,
    "ANALYTIC_HOLE_MIN_SIDES": 12,
    "ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO": 0.002,
    "ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS": 0.005,
    "ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT": 0.1,
    "STL_FILE_EXTENSION": ".stl",
    "THREE_MF_FILE_EXTENSION": ".3mf",
    "OBJ_FILE_EXTENSION": ".obj",
    "IGES_FILE_EXTENSION": ".igs",
    "AMF_FILE_EXTENSION": ".amf",
    "STEP_FILE_EXTENSION": ".stp"
}
```

</details>

### Run behavior

| Key | Default | Description |
|-----|:-------:|-------------|
| `DEFAULT_REDUCTION_PERCENT` | `0` | Default percentage of triangles to remove. Numeric `0` leaves automatic reduction available; string `"0"` selects no reduction. A string such as `"25,50,75"` requests multiple variants. |
| `AUTO_REDUCTION_ENABLED` | `true` | Automatically reduce oversized meshes when no explicit reduction was selected. |
| `AUTO_REDUCTION_TARGET_TRIANGLES` | `50000` | Triangle target for automatic reduction. It applies above this count and remains subject to geometry checks and reduction limits. |
| `ASK_FOR_REDUCTION` | `true` | Ask for a common reduction after the update check. An empty answer selects per-model questions. Applies only in an interactive terminal without explicit reduction or dry-run. |
| `SKIP_UP_TO_DATE_OUTPUTS` | `true` | Skip nonempty, current outputs using source, converter, environment specification, configuration, and preview timestamps. See Output names and existing files for overrides. |
| `DEFAULT_STEP_FORMAT` | `"ap203"` | Default STEP format: `ap203`, `ap214`, or `ap242`. Overridden by `--format`. |
| `GENERATE_PNG_PREVIEW` | `true` | Render a `.png` preview alongside each exported STEP file. |
| `INPUT_FOLDER_NAME` | `"models"` | Relative project folder scanned for inputs when no file arguments are provided. Absolute paths, drive-qualified paths and parent traversal are rejected. |

### Mesh inspection and repair

| Key | Default | Description |
|-----|:-------:|-------------|
| `CHECK_MESH_QUALITY` | `true` | Check boundaries, non-manifold edges, connected parts, watertightness, and self-intersections. |
| `REPAIR_MESH_BEFORE_CONVERSION` | `true` | Enable nearby-vertex merging and configured mesh repair operations. Basic validation and exact duplicate/degenerate triangle cleanup still run when disabled. |
| `REPAIR_INTERSECTING_MESH` | `true` | Automatically attempt volume repair of detected closed damaged meshes. Only affected inputs skip reduction and receive repaired output names; clean meshes keep ordinary reduction controls. IGES follows the ordinary CAD pipeline. |
| `MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO` | `0.005` | Maximum fraction of source surface area removed by a unique attached-surface correction before volume repair. Triangle counts include all faces of removed patches. Zero disables this correction; values must be between zero and one. |
| `VERTEX_MERGE_DISTANCE` | `0.0` | Maximum distance for merging nearby vertices. `0.0` selects a conservative scale-aware distance. |
| `FIX_TRIANGLE_ORIENTATION` | `true` | Orient connected triangles consistently when the mesh is orientable. |
| `REMOVE_NON_MANIFOLD_TRIANGLES` | `false` | Remove small neighboring triangles until every edge has at most two faces. This changes geometry. |
| `REJECT_NON_MANIFOLD_MESH` | `false` | Reject detected non-manifold edges that are not handled by volume repair. With quality inspection active, solid mode also rejects non-manifold vertices and non-orientable meshes. |
| `FILL_SMALL_MESH_HOLES` | `false` | Ask trimesh to fill simple small mesh holes. This changes geometry. |
| `CHECK_SELF_INTERSECTIONS` | `true` | Diagnose triangle intersections within the scan limit. Automatic volume repair also requests this scan. Cross-component overlaps are reported separately. |
| `SELF_INTERSECTION_CHECK_MAX_TRIANGLES` | `50000` | Skip the expensive intersection scan above this triangle count. `0` removes the limit. |
| `REJECT_SELF_INTERSECTING_MESH` | `false` | Reject detected internal intersections when they are not handled by volume repair. Solid mode also rejects them regardless of this flag. Scan limits still apply. |

### CAD processing and validation

| Key | Default | Description |
|-----|:-------:|-------------|
| `SEWING_TOLERANCE` | `0.01` | Requested maximum sewing distance in model units. Scale-aware sewing can use a smaller value for mesh inputs. |
| `USE_SCALE_AWARE_SEWING_TOLERANCE` | `true` | Scale the sewing tolerance to model size while treating `SEWING_TOLERANCE` or `--tolerance` as the maximum. |
| `SCALE_AWARE_SEWING_TOLERANCE_RATIO` | `0.000001` | Model bounding-box diagonal multiplier used by scale-aware sewing. |
| `PLANAR_MERGE_ANGLE_RADIANS` | `0.01` | Angular tolerance in radians for merging co-planar faces. `0.01` is about 0.57 degrees. |
| `SEWING_TIMEOUT_SECONDS` | `1800` | Maximum seconds for sewing, volume repair and analytic hole reconstruction workers. |
| `CAD_OPERATION_TIMEOUT_SECONDS` | `300` | Maximum seconds for each fixing, gap filling, solid construction and face refinement worker. |
| `SEW_PARTS_SEPARATELY` | `true` | Sew disconnected mesh parts independently to avoid cross-part edge matching. |
| `FILL_SMALL_PLANAR_BREP_GAPS` | `true` | Close small planar gaps after sewing and before solidification. |
| `MAX_BREP_GAP_EDGE_COUNT` | `8` | Maximum boundary-edge count for one automatically closed planar B-Rep gap. |
| `MAX_BREP_GAP_AREA_RATIO` | `0.005` | Maximum total filled area relative to the open shell area. `0.005` means 0.5 percent. |
| `REQUIRE_SOLID_OUTPUT` | `true` | Require at least one valid solid and reject every free topology component outside those solids. |
| `VALIDATE_STEP_AFTER_WRITING` | `true` | Read the temporary STEP file back and validate it before replacing an existing output. |

### Reduction checks

| Key | Default | Description |
|-----|:-------:|-------------|
| `PRESERVE_BOUNDARIES_DURING_REDUCTION` | `true` | Protect boundary edges and reject reductions that break watertightness, change component count, or change a component's Euler characteristic (for example, fill a handle). |
| `REDUCTION_BOUNDARY_WEIGHT` | `10.0` | Open3D quadric-decimation weight assigned to boundary vertices. |
| `MAX_REDUCTION_SIZE_CHANGE_PERCENT` | `0.5` | Maximum shift of each component's bounding-box limits, as a percentage of that component's diagonal. Checks position and size. |
| `MAX_REDUCTION_VOLUME_CHANGE_PERCENT` | `2.0` | Maximum enclosed-volume change for the mesh and its measurable components. |
| `MAX_REDUCTION_SURFACE_DEVIATION_PERCENT` | `0.1` | Sampled displacement limit as a percentage of the source diagonal. Both directions test vertices, triangle centers, and edge midpoints. A numerical distance floor still applies; this is not a certified maximum over the entire surface. |

### Analytic fitting

| Key | Default | Description |
|-----|:-------:|-------------|
| `RECONSTRUCT_ANALYTIC_PRIMITIVES` | `true` | Replace confidently fitted complete spheres, cylinders, and cones with exact CAD primitives. |
| `ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO` | `0.001` | Maximum radial surface fitting error relative to radius for spheres and to the bounding diagonal for cylinders and cones. Lower values are more conservative. |
| `ANALYTIC_PRIMITIVE_MIN_TRIANGLES` | `32` | Minimum triangle count before analytic primitive reconstruction is attempted. |
| `ANALYTIC_PRIMITIVE_MAX_VOLUME_CHANGE_PERCENT` | `0.1` | Maximum mesh-to-primitive volume difference. A fit that exceeds this limit keeps the faceted B-Rep. |
| `RECONSTRUCT_ANALYTIC_THROUGH_HOLES` | `true` | Replace safely matched faceted straight through-holes with analytic cylindrical cuts. |
| `RECONSTRUCT_ANALYTIC_BLIND_HOLES` | `true` | Replace safely verified faceted straight blind holes with flat-bottomed analytic cylindrical cuts. |
| `ANALYTIC_HOLE_FIT_ERROR_RATIO` | `0.002` | Maximum relative radial error when fitting an opening to a circle. |
| `ANALYTIC_HOLE_MIN_SIDES` | `12` | Minimum polygon side count for each opening. |
| `ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO` | `0.002` | Maximum relative radius difference between the two fitted openings. |
| `ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS` | `0.005` | Maximum angular mismatch between opening normals and the tunnel axis. |
| `ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT` | `0.1` | Global volume-change backstop for one analytic hole replacement. Through-holes and blind holes also use a much tighter local expected-removal check. |

### Experimental reconstruction

| Key | Default | Description |
|-----|:-------:|-------------|
| `EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION` | `false` | Try exact complete linear-extrusion reconstruction before falling back to the stable triangle B-Rep pipeline. |
| `EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO` | `0.0005` | Maximum scale-relative profile and side-wall fitting error for experimental reconstruction. Lower values are more conservative. |
| `EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT` | `0.1` | Maximum permitted volume difference between the repaired mesh and an experimental reconstructed extrusion. |

### File suffixes

| Key | Default | Description |
|-----|:-------:|-------------|
| `STL_FILE_EXTENSION` | `".stl"` | STL input suffix. |
| `THREE_MF_FILE_EXTENSION` | `".3mf"` | 3MF input suffix. |
| `OBJ_FILE_EXTENSION` | `".obj"` | OBJ input suffix. |
| `IGES_FILE_EXTENSION` | `".igs"` | IGES input suffix. `.iges` is also always accepted. |
| `AMF_FILE_EXTENSION` | `".amf"` | AMF input suffix. |
| `STEP_FILE_EXTENSION` | `".stp"` | Default STEP output suffix. |

---

## Troubleshooting

Read the reported error before changing tolerances or disabling checks. Batch conversion continues after a failed file, and the final summary lists successes, skips, and failures.

| Message or symptom | Meaning and next step |
|--------------------|-----------------------|
| Environment setup or imports fail | Check the download error, disk space, and installation path. The launcher attempts dependency repair automatically; Windows long-path settings can also matter. |
| `sewing failed: ...` or another CAD worker timeout/crash | The operation failed or exceeded its time limit. Inspect the source first. For a clean dense mesh, try a smaller triangle count within the geometry limits. Adjust `SEWING_TIMEOUT_SECONDS` or `CAD_OPERATION_TIMEOUT_SECONDS` only if more runtime is appropriate. |
| `input produced an empty shape` | No usable geometry reached the CAD stage. Check that the source contains valid, nondegenerate geometry. |
| `mesh reduction failed: ...` | All reduction attempts failed the target or geometry checks. Request less reduction or use `--reduce 0`. |
| `volume repair requires a closed triangle surface; open boundaries remain` | Volume repair needs a closed surface. Repair missing faces in a mesh editor; optional small mesh hole filling may help simple gaps. |
| `model repair requires a full intersection scan; ...` | The mesh needs repair, but its scan was skipped. Raise `SELF_INTERSECTION_CHECK_MAX_TRIANGLES` or set it to `0` for no count limit. Large scans can be expensive. |
| `mesh contains ... internal self-intersections` or `mesh contains ... non-manifold edges` | The detected damage was not handled by automatic repair. Check whether repair is disabled, or repair the source externally. |
| `repair cannot classify material near cell face ...` | The damaged surfaces do not establish an unambiguous material region. Correct overlapping or unmatched surfaces in the source. |
| `attached patch correction exceeds the allowed source surface area change` | Removing the attached patch would exceed `MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO`, or correction is disabled. Inspect the patch instead of raising the limit blindly. |
| `solid validation failed: ...` | No valid solid was produced, or free topology remains outside the solids. Inspect open boundaries and invalid surfaces. Surface mode is appropriate only when you intentionally want surface geometry. |
| `input changed during conversion` | The source changed or disappeared after loading started. Wait for the source export to finish and retry. |
| `HOLE FITTING` reports `kept faceted` | No candidate passed every fitting check, or a worker failed. The original faceted hole is retained; this alone is not a conversion failure. |
| `STEP readback ...` | The temporary STEP did not pass import, topology, or geometry comparisons. Keep validation enabled, inspect the input, and consider another STEP schema. |
| `STEP writer failed with status ...` or `temporary STEP output is missing or empty` | Export failed. Check the complete message, disk space, and write permissions. Another schema may help an export compatibility issue. |
| `IGES reader failed with status ...` | OpenCASCADE could not import the IGES file. Check it in CAD and repair or re-export IGES before retrying. |
| `reduction ignored for IGES input` | Expected behavior: IGES already contains B-Rep geometry. Omit `--reduce` for these inputs. |
| Preview reports `not created` | STEP export can still be successful. Check the preview error and output permissions; use `--force` to retry a conversion. |

Increasing `--tolerance` is not a general repair or speed fix. Scale-aware sewing may still choose a smaller value, and a larger effective tolerance can join edges that should stay separate. See [Reduction and sewing tolerance](#reduction-and-sewing-tolerance).

---

## Limitations

- **No recovered feature history.** Output does not recreate sketches, constraints, dimensions, or the original CAD feature tree.
- **Freeform geometry stays faceted.** Exact reconstruction covers only the supported primitives, suitable holes, and experimental linear extrusions. Other curves keep their mesh approximation.
- **Validation does not certify dimensions.** Reduction, fitting, and repair can alter geometry within their limits. A valid solid does not establish the original design intent.
- **Some damage requires external repair.** Large openings, ambiguous material regions, and unsupported damage can prevent conversion.
- **Units need attention.** STL and OBJ coordinates are used as supplied and exported in millimeters. Check the physical size after import.
- **Geometry only.** Source colors, materials, textures, and binary STL color extensions are discarded. 3MF/AMF instances and transforms are applied, but assembly hierarchy and part names are flattened. Only static geometry is converted.
- **IGES has no mesh reduction.** `.igs` and `.iges` contain B-Rep geometry; reduction is ignored with a warning.

---

## Requirements

The launchers provide downloads for these platforms:

| Launcher | Platform and architecture |
|----------|---------------------------|
| `2STEP-Converter.bat` | Windows x64 |
| `2STEP-Converter.sh` | macOS Intel or Apple Silicon; Linux x86_64 or ARM64 |

Dependency availability and minimum OS requirements depend on the pinned conda-forge packages and their platform builds.

You also need:

- Internet access for initial setup and later dependency updates or repairs. The release check can be disabled with `--no-update-check`.
- On macOS/Linux: Bash, `curl`, `awk`, and one SHA-256 tool: `sha256sum`, `shasum`, or `openssl`.
- Write access to the selected environment location, project `data/` folder, configured input folder, and output locations.
- Several GB of free disk space for the environment and package cache, plus room for inputs, temporary files, and outputs. Dense models also need enough RAM for mesh and CAD processing.

---

## Project Structure

```
2STEP-Converter.bat      - launcher for Windows: auto-setup + run
2STEP-Converter.sh       - launcher for macOS / Linux: auto-setup + run
src/
  converter.py           - conversion orchestration and existing script entry point
  cli.py                 - arguments, interactive batches, watch mode and caching
  config.py              - immutable Settings and explicit JSON loading
  version.py             - single source of the installed project version
  update_check.py        - bounded GitHub release check and version comparison
  mesh_readers.py        - STL/OBJ/3MF/AMF readers and lazy IGES loading
  reconstruction.py      - analytic primitive and linear-extrusion fitting
  step_io.py             - atomic STEP export and readback verification
  preview.py             - PNG rendering from exported STEP
  console.py             - per-session console output, progress timers and prompts
  estimator.py           - conversion time history with an explicit storage path
  options.py             - reduction argument parsing and duration formatting
  kernel_io.py           - scoped suppression of native CAD diagnostics
  mesh_geometry.py       - cleanup, mesh diagnostics and reduction fidelity checks
  occ_geometry.py        - topology preservation, nesting and geometry comparisons
  model_repair.py        - volume reconstruction, material/void classification and repair measurements
  brep_operations.py     - checked transport and timeouts for CAD subprocesses
  brep_worker.py         - isolated CAD operations as regular Python functions
  environment.yml        - pinned direct dependency versions
README.md                - this file
commit.md                - release notes for version 4.0.0
post.md                  - Reddit post draft
LICENSE.md               - MIT license
models/                  - input and output folder, auto-created when absent
data/                    - persistent state, auto-created when absent
  config.json            - user settings
  estimator.json         - conversion time history for ETA estimates
docs/                    - screenshots and comparison images used in the README
lib/                     - portable environment when that install mode is selected
```

The launchers invoke `src/converter.py`. Running this Python entry point with `--help` or `--version` exits without loading CAD or numerical dependencies or creating configuration files. The launchers themselves still perform environment setup and import checks before passing these arguments to Python.

### Use from Python

Use the prepared environment and put `src/` on the Python import path. Pass settings explicitly:

```python
from pathlib import Path
from config import Settings
from converter import convert

success, result = convert(
    Path("models/part.stl"),
    Path("models/part.stp"),
    settings=Settings(generate_png_preview=False),
)
if not success:
    raise RuntimeError(result)
```

Library calls use `Settings()` defaults and do not read `data/config.json` automatically. To load it explicitly, use `config.load_config(path, create=False)`, which returns settings and warnings. `settings.with_overrides(...)` returns a new settings value without rewriting the file.

`convert()` does not apply CLI prompts, automatic reduction, or generated output names. Its optional `reduce_fraction` is the fraction to keep: `0.75` removes about 25 percent; `None` keeps all triangles. The default `step_schema` is `AP203`; the other writer values are `AP214IS` and `AP242DIS`.

Calls are silent unless a progress reporter is supplied and do not save timing history unless an estimator is supplied. On success, `result` contains output measurements; on failure, it contains an error string.

---

## Credits

Built on these open-source projects:

- **[OpenCASCADE](https://www.opencascade.com/)** via **[pythonocc-core](https://github.com/tpaviot/pythonocc-core)** - the CAD kernel that does the actual sewing, fixing, and STEP export.
- **[FreeCAD](https://www.freecad.org/)** - the inspiration for the Part workbench pipeline replicated here.
- **[Open3D](https://www.open3d.org/)** - mesh cleanup (dedup, degenerate-triangle removal) and the primary quadric-decimation reducer.
- **[trimesh](https://trimesh.org/)** - optional small mesh hole filling and auxiliary mesh operations. **[fast-simplification](https://github.com/pyvista/fast-simplification)** supplies alternative reduction attempts when Open3D fails or its output fails geometry checks.
- **[matplotlib](https://matplotlib.org/)** + **[Pillow](https://python-pillow.org/)** - the wireframe `.png` preview renderer.
- **[micromamba](https://mamba.readthedocs.io/)** - portable conda-compatible environment manager that bootstraps the whole stack.

---

## Disclaimer

This software is provided **"as is"**, without warranty of any kind. The converter itself has been written and tested in good faith, but it relies on a large set of third-party packages (OpenCASCADE, Open3D, Qt6, VTK, MKL, trimesh, fast-simplification, and others) that the launcher downloads automatically from the public conda-forge channel on first run. I have no control over those packages and cannot guarantee their correctness, stability, or that they will never ship a bug or harmful change in a future version.

By running the launcher you accept that:

- Output STEP files may contain errors, invalid topology, or geometry that differs from the source. Always inspect critical results in your CAD tool before relying on them.
- Transitive dependency builds can still change when conda-forge republishes compatible solver results; review environment transactions before using the converter in a critical workflow.
- I am not responsible for any data loss, incorrect output, system instability, or other damages resulting from use of this software or its dependencies.

The full legal text is in [LICENSE.md](LICENSE.md).

## Authorship and AI Assistance

This project is my own work and was designed, developed, and tested by me. I made the implementation decisions and remain responsible for the code and its results. During development, I also used Anthropic Claude and OpenAI Codex as supporting tools to better understand unfamiliar topics, check calculations, explore possible solutions, review parts of the code, and improve documentation. These tools assisted the development process, but they did not replace my own coding skills, judgment, testing, or responsibility for the project.

## License

[MIT](LICENSE.md) (c) 2026 [YaneonY](https://github.com/yaneony/2STEP-Converter)
