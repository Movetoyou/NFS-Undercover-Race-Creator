NFS UNDERCOVER RACE CREATOR
STEP-BY-STEP GUIDE - EDITOR + QUICK RACE DATABASE PATCHER
========================================================

This guide covers the current nfs-race-creator.py application, run.bat,
and the separate Patch Quick Race Database.py / .bat tools.

THE COMPLETE WORKFLOW
---------------------
Prepare backups -> configure the editor -> create a project -> capture
points -> set the map marker -> configure registration -> export ->
import gameplay with VltEd -> import names with Binary -> patch the profile
seed for your event if needed -> create a new profile -> test in the game.

Saving a project, exporting scripts, and installing a race are DIFFERENT
operations. The game does not load .nfsrace, .nfsms, or .end files directly.

E675 and sp_c_45 are EXAMPLE INPUTS throughout this guide, not required
values. Replace them with your chosen Event ID and Track / vault name in
fields, paths, filenames, localization keys, and patch commands. Check
that your chosen names are unused before installing a new race.

The editor supports Sprint, Circuit, and Checkpoint events. The separate
database patcher registers the Event ID you specify with PATCH <EventID>.
It does not automatically read the event from your Race Creator project,
and the track name is not an input to the database patcher.

STEP 1 - PREPARE THE TOOLS AND BACKUPS
-------------------------------------
1. Install Python 3.11 or newer for Windows, including the Python launcher
   (the "py" command) and Tcl/Tk support. The patcher also requires Python's
   SQLite serialization support.
2. Open Command Prompt and install the editor's dependencies:

     py -3 -m pip install numpy Pillow scipy

   The patcher itself needs no additional Python packages.
3. Obtain versions of NFS-VltEd and Binary that support NFS Undercover.
   These are separate tools; Race Creator does not run their imports.
4. Keep the application folder together:

     run.bat                         Main editor launcher
     nfs-race-creator.py              Main editor
     Patch Quick Race Database.bat   Separate patcher launcher
     Patch Quick Race Database.py    Selected-event seed patcher
     assets\                         Map, icons, template
     projects\                       Editable race projects
     exports\                        Generated installation scripts
     settings.json                   Paths, hotkeys, HUD settings
     theme.json                      Editor appearance
     hud_layout.json                 HUD layout
     logs\                           Editor logs

5. With the game closed, back up the game's GLOBAL and LANGUAGES folders
   before installing a race. Keep a backup of your profile saves too.
   Keep these backups separate from the working game installation.
6. Preserve any important TRACKS\*.hot files before capturing. Capture
   uses the game's own hot-position slot and can overwrite its old content.

Live capture expects the supported NFS Undercover v1.0.0.1 executable
signature. Unsupported builds disable capture. The database patcher has
its own executable-layout check; not every game release is supported.

STEP 2 - LAUNCH RACE CREATOR
----------------------------
1. Double-click run.bat.
2. The window contains these tabs:
   Race, Points, Map, Export, Advanced, Setup, and Log.
3. If the window does not open, run this from the application folder in
   Command Prompt so that you can read any startup error:

     py -3 -B "nfs-race-creator.py" --verbose

You can work on a project and edit existing points without running the
game. Live capture requires the game to be running.

STEP 3 - CONFIGURE PATHS, HOTKEYS, AND THE HUD
--------------------------------------------
1. Open Setup.
2. Next to "Game folder", click Browse... and select the folder containing
   nfs.exe, GLOBAL, TRACKS, and LANGUAGES. Do not select GLOBAL itself.
   Use Browse... so the selected path is saved for subsequent exports.
3. Check "Hot slot (1-5)". The default is 5; choose another slot if needed.
4. Review "Global hotkeys", then click Apply hotkeys if you change them.
   The combinations actually displayed in Setup are the ones to use.
   Saved settings or automatic conflict handling can change the defaults.
   An empty hotkey disables that action's keyboard shortcut.
5. Optional: enable Show HUD and click Apply HUD settings.
6. If the HUD is hidden behind exclusive fullscreen, close the game first,
   then use Game > Launch game (windowed). Enable "Borderless game window"
   and use Apply borderless now when necessary.

Built-in default capture hotkeys:
  Ctrl+Alt+S   Set start grid
  Ctrl+Alt+F   Set finish line
  Ctrl+Alt+C   Add checkpoint
  Ctrl+Alt+V   Add time bonus checkpoint
  Ctrl+Alt+H   Add shortcut
  Ctrl+Alt+R   Set reset trigger
  Ctrl+Alt+M   Set TrackPosition from the car
  Ctrl+Alt+Z   Undo last capture
  Ctrl+Alt+O   Show/hide HUD
  Ctrl+Alt+E   Bring editor to front
  Ctrl+Alt+X   Quick export

Check Setup rather than assuming these defaults are still assigned.
Use the Export tab's validation before exporting; quick export does not
show the normal validation confirmation.

STEP 4 - CREATE OR OPEN A PROJECT
---------------------------------
1. Click New, or use File > New project (Ctrl+N).
   To resume an existing race, use Open / File > Open project... (Ctrl+O)
   and select its .nfsrace file instead.
2. Open Race and fill in Identity:
   - Event type: start with Sprint for a simple first race.
   - Event ID: for example, E675.
   - Region: select the region where the race is located.
   - Track / vault name: for example, sp_c_45, or use Suggest.
3. Verify in VltEd that the event ID and full track path are unused before
   installing them as NEW nodes. Neither Suggest nor validation checks
   the game for naming collisions.
4. Click Save (Ctrl+S), choose a location in projects, and keep the .nfsrace
   extension. Use File > Save project as... to make a separate copy.

Example used in this guide:
  Event type:         Sprint
  Event ID:           E675
  Region:             Palm Harbor (region_c)
  Track / vault name: sp_c_45
  Event path:         race_bin_career/E675
  Track path:         tracks/region_c/sprint/sp_c_45

Use this example only if those nodes are unused in your installation,
or if you are deliberately editing your existing E675 race.
Event IDs must be E followed by at least three digits. The template
recommends 660 or greater, but that does not guarantee an ID is available.
Track names may contain only letters, digits, and underscores.

STEP 5 - FILL IN THE RACE SETTINGS
----------------------------------
1. In Race, set the English and Russian event titles.
2. Set Unlock level, Cash, Cash on dominate, XP, and XP on dominate.
3. Enter Domination time and Track record in seconds.
4. For a Checkpoint event, set a positive Time limit and configure time
   bonus points later. A basic Sprint does not need a countdown limit.
5. Enter Track length yourself. The displayed road-route preview does not
   automatically calculate or fill this field.
6. Configure Traffic level, Cops in race, Threat range, and the AI min/max
   values. Keep each minimum no higher than its maximum.
7. For a normal non-Collectors-Edition test race, turn OFF
   "Collectors Edition race". It is ON in the new-project defaults.
   Leave Master event off unless you specifically need it.
8. Keep a suitable Intro NIS, such as the default Race01.

Circuit note: the current Race tab does not expose a lap-count control.
The exported NumLaps comes from the project's num_laps value (default 1).
For a multi-lap circuit, set/check NumLaps on your custom event in VltEd
before saving the installed gameplay data.

STEP 6 - SET THE LOCALIZATION KEY
---------------------------------
1. For the example E675 race, enter this exact "Localization key" in Race:

     Event_Name_E675

   Replace E675 with your own event ID when documenting another race.
   Preserve the key's spelling and case.
2. Leave Advanced > "Localization entries" empty for this simple setup.
   The exporter then uses the Race tab's titles and the explicit key.
3. In Advanced > "Endscript (.end) settings", check:

     English language vault: LANGUAGES\English_Global.bin
     Russian language vault: LANGUAGES\Russian_Global.bin
     String collection:      STRBlocks
     String block:           Global

   "Global" is case-sensitive; do not change it to GLOBAL.
4. Disable "Also write Russian strings" if that language vault is not
   present or you do not want it changed.

For multiple keys, use one Advanced localization line per entry:
  KEY | English text | Russian text
For example:
  Event_Name_E675 | Harbor Sprint | Harbor Sprint

Nonempty Advanced entries replace the simple Race-tab localization list.
If both the explicit key and Advanced entries are blank, the current
exporter generates TRACKNAME_<track> and RACE_<event> instead. Those keys
alone do not provide the normal Undercover Event_Name_<event> title.

STEP 7 - CAPTURE THE ROUTE IN THE GAME
-------------------------------------
1. Start Undercover, load a profile, and spawn the car in free-roam or a
   race. Keep it unpaused and out of menus/loading screens.
2. In Race Creator, click Attach, then return focus to the game.
3. Drive to the intended start position, face the race direction, and use
   the "Set start grid" hotkey from Setup.
4. Drive along the intended course. At each required gate, face the travel
   direction and use "Add checkpoint". Capture them in driving order.
5. At the end of the course, face the finish direction and use
   "Set finish line".
6. Capture a Reset point at the desired reset-player trigger location.
7. If needed, capture Time bonus points and Shortcut points.
8. Check the HUD/status after each capture and wait for it to finish
   before requesting another capture.

Start, Finish, and Reset captures replace their existing point.
Checkpoint, Time bonus, and Shortcut captures append a point.
The top toolbar has equivalent capture buttons, but live capture requires
the game window to be focused; hotkeys are the practical in-game method.

Limits:
  Start: 1; Finish: 1; Reset: 1.
  Regular and time bonus checkpoints: no more than 15 each.
  Shortcuts: no more than 30.

Important: regular checkpoints and time bonus checkpoints are stored as
separate lists. The export orders ALL regular checkpoints before ALL
bonus checkpoints, not by interleaved capture time. For a simple timed
course, use an ordered sequence of time bonus points rather than assuming
that alternating regular/bonus captures produces an alternating sequence.

The app triggers the game's own .hot capture and reads the saved position.
It copies world X/Y/Z and converts rotation to degrees. A capture can
replace the selected hot slot; it does not require a separate capture mod.

STEP 8 - REVIEW AND EDIT THE POINTS
-----------------------------------
1. Open Points and select a point in the list.
2. Review X, Y, Z, and Rotation. Z is the point's world height.
3. For finish/checkpoint gates, review Dimensions X/Y/Z. For finish/bonus
   points, review Flare spacing. Set Time bonus for bonus points and
   Reset width for the reset trigger.
4. Click Apply after editing the selected point's fields.
5. Use Move up / Move down to reorder points within their own list.
   These buttons do not interleave regular and time bonus lists.
6. Use Delete for an unwanted point or Undo for a recent point operation.
7. Save the project again.

Reading an already saved hot slot:
1. Set Game folder and Hot slot in Setup.
2. Click "Read hot slot now", or use Game > Read hot slot.
3. This reads an existing position from TRACKS\*.hot into "last capture";
   it does not trigger a new capture or append a new project point.
4. To use it, select an EXISTING point in Points and click Use last capture.
   That updates the selected point's position and rotation.

STEP 9 - PLACE THE EVENT'S MAP MARKER
------------------------------------
1. Open Map.
2. Click Use start position to place TrackPosition on the captured start.
   Alternatively, left-click the desired marker location, or use
   Use car position with the live-capture conditions satisfied.
3. Review Map X and Map Y under Race > TrackPosition (map marker).
   These use the 0..3072 map frame, not the car's world X/Y coordinates.
4. Use Recalculate route to refresh the road-route preview if needed.
   Inspect the course for unexpected shortest-path shortcuts; add or
   reposition checkpoints to guide the intended race.
5. Save the project.

Map controls: left-click places TrackPosition, right-drag pans, mouse
wheel zooms, and Fit map resets the view. Rotate left/right and Flip Y for
display change the view, not your stored point coordinates.

The editor route is a preview. Exporting it does not install a minimap
texture or modify TRACKMAPS. A visible marker in the editor is not proof
that the game's race registration or profile unlock entry exists.

STEP 10 - CONFIGURE REGISTRATION SAFELY
---------------------------------------
1. Close the game before inspecting/installing game data in VltEd.
2. Open the SAME game installation in VltEd.
3. Inspect the relevant arrays and record their CURRENT element counts.
4. In Race Creator, open Advanced > Registration and unlocks.
5. For a NEW normal Quick Race event, configure:
   - Append event to race_bin_career.
   - Append track to tracks/<region>/<type>.
   - Unlock in quick race.
   Enter the correct current count beside each enabled option.
6. Enable "Add career logic trigger (map marker)" only if you also want
   career registration. It requires the event in race_bin_career and the
   current mw2_career/unlock Children count. Its gate uses Unlock level.
7. Enable the Collectors Edition unlock option only when intentionally
   targeting that separate unlock path; do not enable it by habit.

The actual target arrays are:
  Append event:
    race_bin_career -> Children
  Append track:
    tracks/<region>/<type> -> Children
  Normal Quick Race:
    mw2_career/unlock/quick_race/unlock_quickrace -> UnlockEvents
  Collectors Edition Quick Race:
    mw2_career/unlock/collectors_edition/unlock_quickrace -> UnlockEvents
  Career trigger:
    mw2_career/unlock -> Children

The Quick Race count fields are labeled "Children count" in the GUI,
but their actual target field is UnlockEvents.

CRITICAL: enter the count BEFORE adding the new item, not the last index.
For example, an array containing 199 items has indices 0..198. Enter 199;
the generated script resizes it to 200 and writes the new item at 199.
This is only an example, NOT a universal value for every game installation.

Wrong counts can shrink arrays or overwrite existing registrations.
Registration is off by default for this reason. Do not guess counts or
reuse someone else's saved counts without checking your game data.
If a node is already registered, do not append it again. See Step 17.
The game-data checker does not determine these counts for you.

STEP 11 - VALIDATE AND EXPORT
-----------------------------
1. Click Save to preserve your editable .nfsrace project.
2. Open Export and check Output folder. It may contain a saved path from
   another installation; use Browse... to select the intended folder.
3. Set Base file name, or click Auto name.
4. Click Refresh preview and read Validation.
5. Fix all ERROR messages and review every warning. Do not use
   "Export anyway" as a substitute for fixing an invalid race.
   Check registration counts separately even when validation is clear.
6. Inspect the .nfsms, VERSN2 main, and VERSN1 launcher previews.
7. Click Save both, or use the top Export button.
8. Click Open folder and confirm the three files are present.

Example with base name E675_sp_c_45:
  E675_sp_c_45.nfsms          Gameplay installation script for VltEd
  E675_sp_c_45.end            Main VERSN2 language script for Binary
  E675_sp_c_45_Launcher.end   VERSN1 launcher for Binary

Keep the two .end files together. The launcher references the main file.
Export writes/replaces files with the selected names, but does NOT import
or save the game's gameplay/language data and does NOT run the patcher.

Current button behavior: Save .end (V1+V2) writes only the language pair.
Save .nfsms also writes both .end files in the current implementation.
Use Save both for the complete installation set.

STEP 12 - INSTALL THE GAMEPLAY SCRIPT WITH VLTED
------------------------------------------------
1. Keep the game closed and confirm your GLOBAL backup exists.
2. Open the intended Undercover installation in NFS-VltEd.
3. Use VltEd's mod/script import command to import the exported .nfsms.
4. Read the import results. If there are errors, investigate before saving
   a partially imported installation.
5. Inspect your event, track layout, points, and enabled registrations.
   For the example, check race_bin_career/E675 and
   tracks/region_c/sprint/sp_c_45.
6. Review the current exporter's reward-reference limitation:
   RewardsForWinner and RewardsForDominate default to the text "0".
   That text is a node reference, NOT a null value. Clearing the editor's
   reward boxes restores "0" rather than creating empty reward arrays.
   If you want no reward-node references, remove those two fields from
   YOUR custom event in VltEd before saving, or supply valid existing
   reward nodes. Do not remove fields from stock events globally.
   The separate Cash and XP values are not those reward-node references.
7. For a Circuit, check/set NumLaps as described in Step 5.
8. Save the modified gameplay data in VltEd, then close the editor.

Copying .nfsms to the game folder without importing and saving does nothing.
The game needs the saved GLOBAL\gameplay data, not the exported script.

STEP 13 - INSTALL THE RACE NAMES WITH BINARY
-------------------------------------------
1. Keep the game closed and confirm your LANGUAGES backup exists.
2. Start Binary in User mode.
3. Use Import Endscript and select E675_sp_c_45_Launcher.end (or your
   corresponding *_Launcher.end), NOT the main VERSN2 .end file.
4. Confirm/select the correct game installation when requested. The
   launcher contains the game directory saved in Race Creator; regenerate
   it or correct that selection if you moved the installation.
5. Let the launcher load the main .end file and apply its language strings.
6. Review errors and save the changes when prompted.

If Binary reports a string-block error, check STRBlocks / Global and the
language paths in Advanced. Disable Russian export if its vault is absent.
Do not import the .nfsms into Binary; gameplay import belongs in VltEd.

STEP 14 - CHECK THE INSTALLED GAME DATA
--------------------------------------
1. Return to Race Creator with the correct project open.
2. In Export, click Check game data.
3. Review whether the current Start grid, TrackPosition, and checkpoint
   coordinate patterns are found in GLOBAL\gameplay.lzc / gameplay.bin.
4. If missing, check the game path and whether VltEd actually saved.

This is a coordinate-pattern check, not a full semantic vault verifier.
It does not validate registration arrays, language keys, the profile
unlock database, or in-game marker rendering. Compressed data can also
prevent raw coordinate patterns from being found.

STEP 15 - APPLY THE DATABASE PATCHER FOR YOUR EVENT
--------------------------------------------------
The patch command is PATCH <EventID>. Here <EventID> means the Event ID
from your Race Creator project; do not type brackets or parentheses.
E675 is only an example, and sp_c_45 is only an example track name.
The database patcher needs the Event ID, NOT the Track / vault name.

Why it is needed: Quick Race uses entries in the profile's UserUnlocks
SQLite database. Adding an event to a gameplay unlock list cannot create
a missing database row. The patcher adds that row to GLOBAL\NFSGD.BIN,
which supplies the database for NEW profiles.

1. Finish the VltEd/Binary installation steps first. Ensure your chosen
   event is registered in the intended gameplay unlock list.
2. Close the game and all game-data/database editors.
3. For the default game location, double-click:

     Patch Quick Race Database.bat

   The DEFAULT patcher location is:
     D:\Games\Need for Speed Undercover\GLOBAL\NFSGD.BIN

   The patcher does not read Race Creator's Setup/settings.json paths.
4. Verify that the displayed seed path belongs to the intended game.
5. Type the command for YOUR event and press Enter. For example:

     PATCH E675

   For a different Event ID, such as E676, type PATCH E676 instead.
   PATCH and the E in the ID must be uppercase; the ID needs at least
   three digits. PATCH alone, a track name, or PATCH (E675) is invalid.
   Press Enter with an empty command to cancel without seed changes.
   Invalid commands are rejected without creating a backup or applying.
6. The patcher computes the short Event ID's hash, verifies the database,
   displays the row to add, creates a backup, and applies that event's
   patch. With this direct-command workflow, PATCH <EventID> authorizes
   application; there is no second confirmation prompt.
7. Wait for "Patch verified for <EventID>" and keep the printed backup path.
   The BAT window pauses so you can read the result.

Each new row is Type=0, State=0 (locked). The Key depends on your Event ID.
For the E675 EXAMPLE ONLY, it is 3304766100 (0xC4FABA94).
A valid command registers a database key; it does not prove the race is
installed in gameplay or add it to any gameplay unlock lists.

To see an EVENT-SPECIFIC preview before confirming, open Command Prompt
in the application folder and run (replace E675 with your own ID):

  py -3 -B "Patch Quick Race Database.py" --event-id E675

This prints the selected event's row preview first, then requires the
matching PATCH E675 command. A command naming a different event is rejected.

For a different game location, replace the example paths below:

  py -3 -B "Patch Quick Race Database.py" --seed "E:\Games\Need for Speed Undercover\GLOBAL\NFSGD.BIN" --exe "E:\Games\Need for Speed Undercover\nfs.exe"

This still asks for PATCH <EventID>. Add --event-id YOUR_ID if you want
that event's preview before confirmation.

To preview without writing ANY patcher files, provide --event-id AND
--dry-run. For the example ID at the default game location:

  py -3 -B "Patch Quick Race Database.py" --event-id E675 --dry-run

Combine those options with --seed and --exe for another installation.
--dry-run does not ask for a PATCH command or create a backup.

Patcher options:
  --event-id ID  Select the event to preview; no default ID is assumed.
  --seed PATH    Select the encrypted GLOBAL\NFSGD.BIN seed.
  --exe PATH     If omitted, nfs.exe is expected one folder above GLOBAL.
  --dry-run      Verify/preview only; requires --event-id.
  --apply        Back up/apply without confirmation; requires --event-id.
  --help         Show command-line help.

Prefer the interactive workflow. --apply and --dry-run cannot be combined.
Do not pass a profile save as --seed; only NFSGD.BIN is supported.

The patcher verifies the database/encryption and automatically creates:
  GLOBAL\NFSGD.BIN.before-<EventID>.<timestamp>.bak
The backup name contains YOUR Event ID. Very long IDs use their first
32 characters plus the event hash in the filename. Existing backups are
not overwritten, and incomplete new backups are removed if creation fails.
The patcher does not modify nfs.exe, install race/track nodes, or read/change
any existing profile save.

If that unlock key already exists as a Quick Race entry in the seed, the
patcher does nothing and preserves its state, including unlocked states.
That message does not say whether an old profile contains the row.

STEP 16 - CREATE A NEW PROFILE AND TEST
---------------------------------------
1. After patching the seed for your chosen Event ID, launch the game and
   create a NEW profile. Keep your old profiles and their existing progress.
2. If your event was appended to the normal Quick Race unlock list in
   Step 10, complete career event E002 in the new profile. For a different
   unlock path, satisfy that path's actual gameplay conditions instead.
3. Open Quick Race and look for YOUR event's marker and localized title.
4. Start the race and check the start direction, checkpoint sequence,
   gate widths/heights, finish, route choices, and any bonus/shortcut points.
5. Check traffic, AI, timers, cash/XP, and circuit laps where applicable.
6. If career registration was enabled, test its intended level gate too.

Patching the seed does NOT update an existing profile, and restoring the
seed later does not rewrite profiles already created from it.
The patcher adds a LOCKED row; it does not instantly unlock the event.

Database/backup behavior was tested on temporary copies. Actual in-game
marker behavior still needs to be checked in your installation.

STEP 17 - EDIT AN INSTALLED RACE WITHOUT DUPLICATING REGISTRATIONS
-----------------------------------------------------------------
1. Open the saved .nfsrace project and use Save project as... if you want
   an independent revision. Back up the current game data again.
2. Make your point/parameter changes, then save and export again.
3. Do NOT blindly import the complete creation script over an installed
   race. It includes add_node commands and registration-array resizing.
4. For nodes already registered, disable the corresponding append options
   before exporting and review the script for an intentional update of
   existing nodes. Preserve your manual VltEd/script edits.
5. For genuinely new registrations, inspect fresh current array counts.
   Do not increment a saved old count and append the same event again.
6. Reapply the required gameplay/language changes, save, and restart the
   game. Recheck reward fields and NumLaps because a fresh export can
   reintroduce the project's older values.

The GUI Undo changes the project only; it does not undo VltEd imports,
Binary changes, or the database patch.

RESTORING BACKUPS
------------------
1. Close the game and all data editors.
2. Keep copies of your current modified files before replacing anything.
3. Restore the intended GLOBAL/LANGUAGES backups to reverse the gameplay
   and language installation. Keep any other mods in mind when choosing
   a backup; a full old folder backup also removes later changes.
4. To undo a seed patch, copy the desired
   NFSGD.BIN.before-<EventID>.<timestamp>.bak into GLOBAL as NFSGD.BIN.
   Keep the original backup too. Show filename extensions in Explorer so
   the restored name does not accidentally remain NFSGD.BIN.bak.
   Each backup is a complete seed snapshot: restoring an older backup also
   removes any event rows added AFTER that snapshot, not just the named ID.

Seed restoration affects the seed, not existing saves. Reinstalling or
replacing NFSGD.BIN can remove added rows; check your event again with
--event-id YOUR_ID --dry-run.

TROUBLESHOOTING
---------------
Editor does not open / missing numpy, PIL, or scipy:
  Run the dependency command in Step 1 using the same "py -3" interpreter.
  Use the console launch in Step 2 to see errors. Missing tkinter requires
  Python's Tcl/Tk component, not a package named tkinter from pip.

Game not attached / permission denied:
  Confirm nfs.exe is running. Match the editor's privilege level to the
  game; use an authorized elevated launch only if it is required.

Capture rejected or timed out:
  Keep the game focused, unpaused, and in actual driving gameplay. Check
  the correct TRACKS folder, supported game build, and selected hot slot.
  Check Setup/Log for reassigned or conflicting hotkeys.

HUD invisible or blocking clicks:
  Use windowed/borderless rather than exclusive fullscreen. Review Show
  HUD and Click-through under Setup, then click Apply HUD settings.

No marker in Quick Race:
  Check saved gameplay data, event/track registration, the intended unlock
  list, TrackPosition, and the profile's database row for YOUR Event ID.
  Confirm the seed was patched BEFORE creating the profile and the relevant
  unlock condition was satisfied (E002 for the normal Quick Race list).
  Do not assume marker coordinates alone create a profile unlock entry.

Missing/wrong race title:
  Check the Event_Name_<event> key, Advanced localization overrides,
  Binary import/save, selected language vault, and exact Global spelling.

Patcher reports unsupported executable, decryption, integrity, or capacity:
  Stop. Check the matching nfs.exe and NFSGD.BIN installation. Do not force
  the operation or manually edit encrypted bytes. Keep the error message.

Patcher interrupted with Ctrl+C during application:
  The seed may already be patched. Keep the printed backup and run
  --event-id YOUR_ID --dry-run to check that event rather than assuming
  cancellation. Replace YOUR_ID with the ID used in your PATCH command.

For editor troubleshooting, open Log or Help > Open log file.
The persistent log is logs\race_creator.log. You can also run:

  py -3 -B "nfs-race-creator.py" --diagnostics

This displays the built-in diagnostic report without opening the GUI.
