# What we learned building this

Notes from building and running anycubic-slicer-mcp against a real Kobra X (firmware 2.0.2.8) and Anycubic Slicer Next 2.0.0.3 on macOS. Most of these cost us a failed print, a wrong number or a confused hour. They're written down so the next change doesn't repeat them.

## The slicer's presets

**The window ignores a user preset unless it looks like one it saved itself.** A preset in `user/<folder>/<kind>/` needs a `"version"` field, and its `.info` file needs `base_id` (the parent system preset's `setting_id`). Without them the window skips the file without saying anything. The binary logs `can not find version, not loading for user preset` and `can not find base_id`. `write_user_preset` now writes both, plus `"is_custom_defined": "0"`.

**A user preset has to inherit straight from a system preset.** The window doesn't follow a chain of user presets. `save_user_preset` rebases onto the system root and copies the in-between differences.

**There's a preset folder per account.** Signed out, the window reads `user/default`. Signed in, it reads `user/<account id>`, named by `app.preset_folder` in `AnycubicSlicerNext.conf`. Signing in or out switches folders, so a preset saved in one vanishes from the window in the other. Presets are now written to every folder.

**The window only reads presets at start-up.** A new preset shows up after a restart, which `restart_slicer` does without force-quitting.

**Kobra X presets keep per-nozzle copies of some settings.** These are `nozzle_temperature_HS` and `_BRASS`, `fan_min_speed_HS`, and so on, and the slicer uses the copies. A preset that only sets `nozzle_temperature` slices at the parent's temperature. The window copies plain values onto the variants; the command line doesn't. `resolve()` now does that copy for every user layer, and new presets carry the copies themselves.

**The real defaults aren't always the visible ones.** "Anycubic PETG @Kobra X" says fan 15 to 80%, but it slices at 30 to 50%, because the `_HS` copies win.

**`default_filament_colour` alone isn't enough on the command line.** Without `filament_colour` the slice comes out orange (#F2754E). The flattened preset now copies one into the other.

**The window's selection lives in `anycubic_presets[0]`** in `AnycubicSlicerNext.conf`: machine, process, `;`-separated filaments and colours. It can be set while the slicer is closed, since the slicer rewrites the file on exit. The file round-trips exactly as `json.dumps(indent=4) + "\n"`. It also holds an encrypted `anycubic_cloud` section, so never print it.

## Reading what the slicer and the window tell you

**The window's time isn't the file's time.** For the same G-code, the Preview tab showed 7 h 31 m, while the file's own Normal-mode estimate (the `M73 ... NormalR` line, in seconds) was 8 h 56 m. Opening a plain `.gcode` shows the file's estimate. Compare like with like.

**Opening a `.gcode.3mf` in the window re-slices it.** The window puts it into Prepare with whatever presets are selected, so its numbers describe a new slice, not the file. Read the G-code header (`; nozzle_temperature = ...`, `; print_settings_id = ...`) to know what a slice really used. The window keeps its latest slice in `$TMPDIR/anycubicslicer_model/.../Metadata/.<pid>.0.gcode`.

**A file name can tell you which settings a print used.** The window names files `..._PETG_0.2_7h30m55s.gcode`, and that time matched one exact settings combination. That's how we worked out what a failed print had been sliced with.

**The filament totals don't match either.** The window counts every forward extrusion, including the filament pushed back after each retraction. The slicer's own figure doesn't. On a 70 m job the difference was about 1 m.

## Safety checks that were wrong

**The post-processing check refused every `.gcode.3mf`.** The INI pattern used `\s*` after `=`, which matched the newline after an empty `; post_process =`. It then read the next line (`; precise_outer_wall = 0`) as a script. Use `[ \t]*` in line-based patterns.

**A test click went through.** A test meant to show `slicer_click` refusing, because we assumed Accessibility permission wasn't granted, actually clicked: the app running the server already had the permission. The click landed harmlessly, but clicks in the title bar (close and minimise buttons) are now refused. Test the refusal with a fake, not the real window.

## The printer over LAN mode

**LAN mode can't be switched on or off during a print, including a paused one.** It also disconnects Anycubic Cloud, which means the slicer window's "Remote Print" can't see the printer ("No available printers found"). `send_print` is the way to print from the computer then.

**Everything arrives twice.** The Kobra X sends each MQTT report twice with the same `msgid`, so drop repeats. Empty-type messages on the `/response` topic are acknowledgements.

**Know the real state names and codes.** `stoped` is the printer's spelling. An idle printer reports `info.state: "free"` and `project: null`, with the last job under `last_project` (its `progress` is 0 for a stopped job). The Kobra X's built-in 4-slot unit is box `-1`, and `loaded_slot: -1` means nothing is in the nozzle. `consumables_percent` is stored, not measured: it read 100% after a 57 m print.

**The camera stream is HTTP-FLV.** ffmpeg's default probing waits about 20 s on it. `-probesize 32768 -analyzeduration 500000` brings a frame down to about 7 s. Opening the stream turns the camera light on, so turn it back off. A dark blob in the frame turned out to be an object resting on the lens.

**Logging leaked the login signature.** httpx logs every request URL at INFO, and the MCP server logs at INFO, so the one-time handshake signature landed in client logs. Keep `httpx` and `httpcore` at WARNING.

**Printing over LAN is a two-step job.**
1. An HTTP multipart POST to the `fileUploadurl` from the `info` report (port 18910, with a one-time `s=` token): a `filename` field, a `gcode` file part and an `X-File-Length` header.
2. A `print/start` message with `taskid "-1"`, `filepath "/"` (the name goes only in `filename`; `"/name"` ends the job with 10111), and `ams_settings.ams_box_mapping` (0-based `ams_index`).

Some firmware wants the slicer topic instead of the web topic, so retry there only after error 10110. These details are community-documented: see the Nino6689/anycubic-lan and printer-protocol-orchard notes. Code from GPL projects was never copied.

## The printer on Anycubic Cloud

**LAN mode and the cloud exclude each other.** Anycubic's Kobra X guide says so: with LAN mode on, the cloud service and the phone app's remote control stop. With LAN mode off, the printer closes port 18910 too, so none of the LAN tools can reach it. Users who want the phone app keep LAN mode off.

**Go through the slicer window, not the cloud API.** Anycubic doesn't publish a cloud API. The community clients reverse-engineered it, need the account token the slicer stores, and the main one is GPL. The slicer window is already signed in, so `cloud_printer_status` and `print_from_slicer` read and press its screens, and the server never sees the login.

**Read the window at full resolution.** OCR on the Retina capture (3024 px wide) took 0.2 s and found every slot label. On the 1600 px copy it took longer and missed one of four. Boxes are scaled down to the image the agent sees afterwards.

**Icons come through OCR as letters.** The Workbench's controls read as `a Skip | 1l Pause | • End`, one line. The click guard splits such rows and works out which part is under the point, so a click on Pause is caught even though OCR never saw "Pause" on its own.

**The Start Print dialog hides a busy printer.** During a cloud print the Workbench lists the printer as Busy, while the dialog says "No available printers found" and "Disconnected", with Start Print greyed out. A greyed button is told apart from an enabled one by colour (pale versus saturated blue).

**The dialog's task name changes every time it opens.** The window prefixes it with `MMDD-HHMM-NN_`. Matching the previewed print against the confirmed one ignores that prefix and compares the rest plus the time.

**In full screen the window has no title bar.** The click guard refused every click in the window's top 30 pt, where a windowed slicer has the close and minimise buttons. In full screen macOS hides that bar above the window, and the slicer's tabs and Remote Print button sit in those 30 pt instead, so every cloud tool was refused. The guard now asks macOS (`AXFullScreen`) and drops the title-bar refusal only when the answer is yes.

**A full-screen window lives on its own Space.** Bringing it forward animates for about a second, and a capture taken during the switch fails. That failure surfaced as a missing Screen Recording permission, which was wrong. The window lookup now waits until the window is on screen, and a failed capture is retried once.

**Don't take a dialog's edges from a centred label.** The Start Print dialog centres the task label with the task name, so a short name like `first_layer_test` moved it right and the parser lost the time, weight and filament to its left. The dialog's title and its Start Print button give both edges.

**A cloud start goes through several stages before it prints.** The Kobra X showed Print Prepare: File Downloading, heating, Auto-leveling (nozzle at 140 °C) and Nozzle purging, then Printing. A 3 min 12 s first-layer test took 14 minutes from pressing Start Print to Free.

**The camera works over the cloud too.** The Workbench has a camera panel with a play button. The stream showed a picture within about 5 seconds.

**After a print the slicer shows a Print Finished card that blocks the window.** While it's open, clicks elsewhere in the window do nothing. It asks for a star rating, so the tools report it and leave it to the user to close.

**The printer keeps its own time estimate.** The window's slice said 8 h 55 m, while the Workbench showed 6 h 2 m total at 37%, then 8 h 38 m an hour later. Don't quote the Workbench total as the job's length.

## Printing what we sliced

**A big flat PETG part can bend the spring-steel sheet off its magnet.** On a 244 × 244 × 10 mm slab, the part held on and pulled the sheet up. The nozzle then dragged into the raised edge and the printer reported a filament clog (11518). More adhesion doesn't help: a brim makes it worse. What helps is less shrinkage: a warmer bed (80 °C), little part cooling, fewer solid skins, and drafts kept off. Our diagnose tool's warping advice assumes the part peels off the bed, so it doesn't fit this case yet.

**Check what a print actually used before comparing settings.** We first compared against the project guide's 25% infill, but the failed print had used the window's defaults (15% infill, 2 walls). The real change was smaller than it first looked.

## Releasing

**GitHub's macOS runners can run out.** Releasing 1.4.1, a macOS arm64 test job sat in the queue for 15 minutes three times running, and GitHub cancelled it each time ("not acquired by Runner of type hosted even after multiple attempts"). That cancelled the whole release, so nothing was published. The release now runs its own tests on Windows and Ubuntu and waits for the tests workflow to pass on the same commit, macOS included. A runner shortage now only delays a release; rerun the cancelled tests jobs, then the release's waiting job.
