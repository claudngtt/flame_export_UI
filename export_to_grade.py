"""
Flame Export to Grade
Version: 1.2.0
Date: June 25, 2026
Author: Henry Claud N'guetta / Harbor
Description:
    This script provides a custom UI action for Autodesk Flame that allows users
    to export shots to a colour grading pipeline. Users select segments from the
    timeline and the script handles EDL export, EXR image sequence export,
    open clip XML authoring, comp v000 creation, and grade/comp track generation.
Requirements:
    - PySide6
    - Autodesk Flame Python API
    - helper.py (Project class)
    - config.py (CONFIG dict)
Usage:
    Loaded as a Flame hook. Appears under Pipeline > Export to Grade
    and Pipeline > Update Open Clip in the timeline custom UI actions.
Changelog:
    1.0.0 - Initial release
    1.0.1 - Fixed crashing on importing comp open clips that don't exist
    1.1.0 - Added selective segment processing for comp and graded track creation
              (click pills in UI to select specific segments; unselected segments excluded)
            - Added Update Open Clip action to force-refresh cached open clip media
            - Fixed segment pills rendering for uncoloured segments (grey fallback)
            - Segment pills are now clickable with blue border on selection
    1.2.0 - Pills now support overwrite selection (green border when overwrite is active)
            - Overwrite flow moved entirely to pill selection, removed list selection
            - Added BFX warning dialog with session-level dont show again checkbox
            - Added pattern_comp_no_version to detect segments missing version number
              and auto-rename to _v001 before export
            - Added job status button showing project name (red/grey toggle)
"""

import flame
import sys
import re
import os
from PySide6 import QtWidgets, QtCore, QtGui
import shutil
from helper import Project
from config import CONFIG
import time


# Module-level


_clipboard_text = []
_bfx_warning_shown = False

class ExportHook:
    """
    Flame export hook that silently overwrites existing files during export.
    Passed to PyExporter.export() via the hooks parameter only when
    the user has explicitly selected shots to overwrite.
    """
    def preExport(self, info, userData, *args, **kwargs): pass
    def postExport(self, info, userData, *args, **kwargs): pass
    def preExportSequence(self, info, userData, *args, **kwargs): pass
    def postExportSequence(self, info, userData, *args, **kwargs): pass
    def preExportAsset(self, info, userData, *args, **kwargs): pass
    def postExportAsset(self, info, userData, *args, **kwargs): pass
    def exportOverwriteFile(self, path, *args, **kwargs):
        return "overwrite"

class CopyWorker(QtCore.QThread):
    """
    Background thread for copying EXR files from bg_plate_L1 to comp_render_main_v000.
    Runs in a QThread to keep the progress dialog responsive during copy.
    Renames files on copy:
        _bg_    → _comp_
        _plate_ → _render_
        _L1_    → _main_
        _v001   → _v000

    Signals:
        progress(int, str): emitted per shot with count and status message
        error(str):         emitted if a shot copy fails
        finished():         emitted when all shots are done
    """
    progress = QtCore.Signal(int, str)
    error    = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, to_copy):
        super().__init__()
        self.to_copy = to_copy  # dict: {src_folder: dst_folder}

    def run(self):
        count = 0
        for src_folder, dst_folder in self.to_copy.items():
            shot_name = os.path.basename(dst_folder)
            self.progress.emit(count, f"Copying {shot_name}...")
            try:
                os.makedirs(dst_folder, exist_ok=True)
                for f in os.listdir(src_folder):
                    if not f.endswith('.exr'):
                        continue
                    new_name = (
                        f.replace('_bg_',    '_comp_')
                         .replace('_plate_', '_render_')
                         .replace('_L1_',    '_main_')
                         .replace('_v001',   '_v000')
                    )
                    shutil.copy2(os.path.join(src_folder, f), os.path.join(dst_folder, new_name))
                count += 1
                self.progress.emit(count, f"Done: {shot_name}")
            except Exception as e:
                self.error.emit(f"{src_folder}: {str(e)}")
        self.finished.emit()



def check_project():
    """
    Validates that the current Flame project has a corresponding path on disk.
    Returns the Project object if valid, None otherwise.
    """
    project = Project()

    if not os.path.exists(project.PRJ_PATH):
        QtWidgets.QMessageBox.critical(
            None,
            "Project Not Found",
            f"Could not find project path:\n{project.PRJ_PATH}"
        )
        return None

    return project

def _warn_bfx():
    """
    Shows a one-time warning per Flame session that BFX cannot be preserved
    by the Python API and will need to be reapplied manually.
    Controlled by a module-level flag that resets when Flame closes.
    """
    global _bfx_warning_shown
    if _bfx_warning_shown:
        return

    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle("BFX Warning")
    layout = QtWidgets.QVBoxLayout(dialog)

    label = QtWidgets.QLabel(
        "Warning: if any segments have a BFX, it cannot be detected\n"
        "or preserved by the Python API.\n\n"
        "If you have BFX on any segments, you will need to\n"
        "reapply them manually after the graded track is created."
    )
    layout.addWidget(label)

    dont_show_cb = QtWidgets.QCheckBox("Don't show again")
    layout.addWidget(dont_show_cb)

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    dialog.exec()

    if dont_show_cb.isChecked():
        _bfx_warning_shown = True

def get_tracks(project_path, segments):
    """
    Collects unique tracks from a list of timeline segments.
    Each segment's parent is its track — using a set deduplicates
    tracks when multiple segments belong to the same track.

    Returns:
        set of PyTrack objects
    """
    tracks = set()

    for segment in segments:
        tracks.add(segment.parent)

    return tracks

def create_EDL(tracks):
    """
    Groups tracks by their parent sequence into a dict.
    Used to build the EDL export UI — one group per sequence,
    one row per track within that sequence.

    Returns:
        dict: {PySequence: [PyTrack, ...]}
    """
    edl_to_create = {}

    for track in tracks:
        version  = track.parent
        sequence = version.parent
        edl_to_create.setdefault(sequence, []).append(track)

    return edl_to_create

def build_color_path(project_path, first_export, comp_updates, layers=None):
    """
    Builds export and open clip paths for all selected segments.
    Checks colour_from_flame on disk to determine if a shot is new or already exists.

    Args:
        project_path:   absolute project root path
        first_export:   list of segment names matching pattern_first_export (e.g. SAC_010_L1)
        comp_updates:   list of segment names matching pattern_comp_update
        layers:         list of segment names matching pattern_layer (L2+), optional

    Returns:
        first_export_paths:  paths for new L1 shots to export
        comp_updates_paths:  paths for new comp update shots to export
        open_clips_paths:    paths for which to create graded open clip XMLs
        existing:            shot names already on disk (shown in UI, skipped by default)
    """
    colour_root = os.path.join(project_path, CONFIG['colour_from_flame'])
    
    first_export_paths = []
    comp_updates_paths = []
    open_clips_paths   = []
    existing           = []

    for name in first_export:
        shot_name = '_'.join(name.split('_')[:2])
        seq       = name.split('_')[0]                           
        shot_dir  = os.path.join(colour_root, seq, shot_name)   
        file_name = f'{shot_name}{CONFIG["first_export_suffix"]}'
        
        clip_name = f'{shot_name}{CONFIG["graded_suffix"]}'
        clip_path = os.path.join(project_path, CONFIG['open_clip_dir'], f'{clip_name}.clip')

        existing_files = [f for f in os.listdir(shot_dir) if f.startswith(file_name)] if os.path.exists(shot_dir) else []

        # New shot — add to export list and queue open clip creation
        if not existing_files:
            first_export_paths.append(os.path.join(shot_dir, file_name))
            open_clips_paths.append(os.path.join(shot_dir, file_name))
        
        # v000 already exported but no open clip exists — ask user whether to create one
        elif existing_files and not os.path.exists(clip_path):
            dialog = flame.messages.show_in_dialog(
                title="Export Already Exists",
                message=f"{file_name} already exists but no grade open clip was found.\nDo you want to create one?",
                type="question",
                buttons=["Create Open Clip"],
                cancel_button="Skip"
            )
            if dialog == "Create Open Clip":
                open_clips_paths.append(os.path.join(shot_dir, file_name))
            existing.append(f"{shot_name}_v000")

        # v000 and open clip both exist — skip entirely, surface in UI as existing
        else:
            existing.append(f"{shot_name}_v000")

    
    for name in comp_updates:
        shot_name = '_'.join(name.split('_')[:2])
        seq       = name.split('_')[0]                           
        shot_dir  = os.path.join(colour_root, seq, shot_name)
        file_name = f'{shot_name}{CONFIG["first_export_suffix"]}'

        clip_name = f'{shot_name}{CONFIG["graded_suffix"]}'
        clip_path = os.path.join(project_path, CONFIG['open_clip_dir'], f'{clip_name}.clip')

        existing_files = [f for f in os.listdir(shot_dir) if f.startswith(name)] if os.path.exists(shot_dir) else []

        if not existing_files:
            # New comp update — add to export
            comp_updates_paths.append(os.path.join(shot_dir, name))

        elif not existing_files and not os.path.exists(clip_path):
            comp_updates_paths.append(os.path.join(shot_dir, name))
            dialog = flame.messages.show_in_dialog(
                title="Export Already Exists",
                message=f"No grade open clip was found.\nDo you want to create one?",
                type="question",
                buttons=["Create Open Clip"],
                cancel_button="Skip"
            )
            if dialog == "Create Open Clip":
                open_clips_paths.append(os.path.join(shot_dir, name))

        else:
            existing.append(f"{name}")
    
    if layers:
        for name in layers:
            shot_name = '_'.join(name.split('_')[:2])
            seq       = name.split('_')[0]                       
            shot_dir  = os.path.join(colour_root, seq, shot_name)
            layer_suffix = name.split('_')[2]  # e.g. L2
            file_name    = f'{shot_name}_{layer_suffix}{CONFIG["first_export_suffix"]}'


            existing_files = [f for f in os.listdir(shot_dir) if f.startswith(file_name)] if os.path.exists(shot_dir) else []

            if existing_files:
                existing.append(f"{name}")
            else:
                dialog = flame.messages.show_in_dialog(
                    title=f"Trying to export a different layer of shot {'_'.join(name.split('_')[:2])}",
                    message=f"Are you sure you want to export {file_name}?",
                    type="question",
                    buttons=["Ok"],
                    cancel_button="Cancel"
                )
                if dialog == "Ok":
                    first_export_paths.append(os.path.join(shot_dir, file_name))
                    open_clips_paths.append(os.path.join(shot_dir, file_name))
                else:
                    continue
                
    
    if not any([first_export_paths, comp_updates_paths, open_clips_paths]):
        return first_export_paths,comp_updates_paths,open_clips_paths,existing


    return first_export_paths, comp_updates_paths, open_clips_paths, existing

def create_grade_first_version(segments):
    """
    Exports a MOV reference clip (grade v000) for each segment to colour_for_flame.
    Used by the colourist as a starting point before any grade is applied.
    Skips shots where a _graded.mov already exists on disk.

    Output path: colour_for_flame/SEQ/SHOT_NAME/SHOT_NAME_comp_render_main_v000_graded.mov
    """

    project = check_project()
    if not project:
        return

    project_name = project.PRJ_PATH
    grade_root   = os.path.join(project_name, CONFIG['colour_for_flame'])


    # PyExporter handles all Flame exports. preset_path points to the user-saved
    # Movie preset that defines codec, resolution and naming.
    # foreground=True means Flame waits for the export to finish before continuing.
    # keep_timeline_fx_renders=False ensures we export the source frames, not cached FX.

    exporter    = flame.PyExporter()
    preset_path = os.path.join(
        exporter.get_presets_dir(
            flame.PyExporter.PresetVisibility.User,
            flame.PyExporter.PresetType.Movie
        ),
        CONFIG['first_export_preset_mov'] + '.xml'
    )
    exporter.foreground               = True
    exporter.keep_timeline_fx_renders = False

    desktop  = flame.project.current_project.current_workspace.desktop
    tmp_reel = desktop.reel_groups[0].create_reel("tmp_grade_v000")

    try:
        for seg in segments:
            name      = str(seg.name).strip("'").strip()
            shot_name = '_'.join(name.split('_')[:2])
            seq       = name.split('_')[0]

            output_dir     = os.path.join(grade_root, seq)
            grade_shot_dir = os.path.join(output_dir, shot_name)
            os.makedirs(output_dir, exist_ok=True)
            existing = [f for f in os.listdir(grade_shot_dir) if f.endswith('_graded.mov')] if os.path.exists(grade_shot_dir) else []
            if existing:
                flame.messages.show_in_console(f"Graded v000 already exists for {shot_name} — skipping", 'info')
                continue


            # match() creates a temporary clip from the segment in the tmp reel
            # so we can export it without touching the original sequence
            tmp_clip = seg.match(tmp_reel, include_timeline_fx=False)
            tmp_clip.name = f'{shot_name}_comp_render_main_v000_graded'
            exporter.export(tmp_clip, preset_path, output_dir)
            flame.messages.show_in_console(f"Exported grade v000: {shot_name}", 'info')

    except Exception as e:
        flame.messages.show_in_console(f"Grade v000 export error: {str(e)}", 'info')
    finally:
        flame.delete(tmp_reel, confirm=False)

def create_graded_open_clip(project_path, export_paths):
    """
    Creates open clip XML files for graded shots in open_clip_dir.
    Open clips are virtual containers that Flame uses to track versioned media —
    the XML defines a ScanPattern so Flame can discover new graded MOVs
    as the colourist delivers versions.

    Skips clips that already have an existing .clip file.
    Handles L2+ layers by deriving clip_name from the layer suffix in the path.

    Output path: open_clip_dir/SHOT_NAME_comp_render_main.clip
    ScanPattern:  colour_for_flame/SEQ/SHOT_NAME/CLIP_NAME_v{version}_graded.mov
    """

    open_clip_dir = os.path.join(project_path, CONFIG['open_clip_dir'])
    grade_root    = os.path.join(project_path, CONFIG['colour_for_flame'])
    created       =  []

    os.makedirs(open_clip_dir, exist_ok=True)

    for path in export_paths:
        
        shot_name  = os.path.basename(os.path.dirname(path))
        clip_name  = f'{shot_name}{CONFIG["graded_suffix"]}'
        base_name  = os.path.basename(path)
        
        # Check if this is a layer above L1
        layer_match = re.search(r'_L([2-9]\d*)', base_name)
        
        if layer_match:
            clip_name = base_name.replace(CONFIG['first_export_suffix'], CONFIG['graded_suffix'])
        else:
            clip_name = f'{shot_name}{CONFIG["graded_suffix"]}'


        clip_path  = os.path.join(open_clip_dir, f'{clip_name}.clip')


        if os.path.exists(clip_path):
            flame.messages.show_in_console(f"Skipped (already exists): {clip_name}.clip", 'info')
            continue

        scan_pattern = os.path.join(
            grade_root,
            shot_name[:3],
            shot_name,
            f'{clip_name}_v{{version}}_graded.mov'
        )

        xml = f'''<?xml version="1.0"?>
<clip type="clip" version="6">
    <handler>
        <name>MIO Clip</name>
        <version>2</version>
        <options type="dict">
            <ScanPattern type="string">{scan_pattern}</ScanPattern>
        </options>
    </handler>
</clip>'''

        with open(clip_path, 'w') as f:
            f.write(xml)

        flame.messages.show_in_console(f"Created openclip: {clip_path}", 'info')
        created.append(f'{clip_name}_graded.clip')

def create_graded_track(track, selected_segments=None):
    """
    Creates a new graded timeline track by copying the selected track to a temp reel,
    replacing each segment's media with the corresponding graded open clip,
    then inserting the modified clip back into the sequence on a new track.

    For each segment:
        - Looks up the open clip at open_clip_dir/SHOT_NAME_comp_render_main.clip
        - Checks colour_for_flame/SEQ/SHOT_NAME for any graded MOVs on disk
        - If graded MOVs exist: replaces media via smart_replace_media, renames
          segment to include '_v<source version>_graded' and sets teal colour
        - If no graded MOVs found: adds to missing_graded_shots, skips

    Shows a dialog at the end listing any shots with missing graded media.
    """
    
    _warn_bfx()

    project = check_project()
    if not project:
        return

    project_name  = project.PRJ_PATH
    open_clip_dir = os.path.join(project_name, CONFIG['open_clip_dir'])

    version  = track.parent
    sequence = version.parent

    desktop  = flame.project.current_project.current_workspace.desktop
    tmp_reel = desktop.reel_groups[0].create_reel("tmp_grade")
    
    # copy_to_media_panel creates a standalone clip from the track in the tmp reel
    # so we can modify its segments before inserting back into the sequence
    tmp_clip = track.copy_to_media_panel(tmp_reel)
    
    progress = QtWidgets.QProgressDialog("Generating grade timeline...", None, 0, len(list(track.segments)))
    progress.setWindowTitle("Create Grade Track")
    progress.setMinimumWidth(500)
    progress.setWindowModality(QtCore.Qt.WindowModal)
    progress.show()
    QtWidgets.QApplication.processEvents()

    missing_graded_shots = []

    count = 0
    for v in tmp_clip.versions:
        for t in v.tracks:
            for seg in t.segments:
                name      = str(seg.name).strip("'").strip()
                shot_name = '_'.join(name.split('_')[:2])
                clip_name = f'{shot_name}_comp_render_main'

                # If specific segments were selected, delete unselected ones from tmp_clip
                if selected_segments and name not in selected_segments:
                    flame.delete(seg, confirm=False)
                    count += 1
                    progress.setValue(count)
                    QtWidgets.QApplication.processEvents()
                    continue

                open_clip_path       = os.path.join(open_clip_dir, f'{clip_name}.clip')
                if os.path.exists(open_clip_path):
                    try:
                        
                        
                        # Check if any graded version exists on disk
                        grade_root  = os.path.join(project_name, CONFIG['colour_for_flame'])
                        grade_dir   = os.path.join(grade_root, shot_name[:3], shot_name)
                        graded_movs = [f for f in os.listdir(grade_dir)] if os.path.exists(grade_dir) else []
                        

                        if not graded_movs:
                            flame.messages.show_in_console(f"No graded version found for {shot_name} — skipping replace", 'info')
                            count += 1
                            progress.setValue(count)
                            QtWidgets.QApplication.processEvents()
                            missing_graded_shots.append(f"{shot_name}")
                            continue
                        
                        # Import the open clip and replace the segment's media
                        # time.sleep gives Flame time to register the import before replacing
                        open_clip = flame.import_clips(open_clip_path, tmp_reel)
                        time.sleep(0.5)
                        seg.smart_replace_media(open_clip[0])
                        seg.name   = seg.name + '_v<source version>_graded'
                        seg.colour = (0.0, 0.4, 0.4)
                        seg.__setattr__('dynamic_name', True)
                        flame.messages.show_in_console(f"Replaced: {name}", 'info')
                        flame.messages.show_in_console(f"Seg type: {seg.type}", 'info')
                    except Exception as e:
                        flame.messages.show_in_console(f"Error {name}: {str(e)}", 'info')
                else:
                    flame.messages.show_in_console(f"Missing: {open_clip_path}", 'info')

                count += 1
                progress.setValue(count)
                QtWidgets.QApplication.processEvents()

    progress.close()

    # Insert the modified clip into a new track at the bottom of the version
    new_track = version.create_track(-1)
    first_seg = list(track.segments)[0]
    if selected_segments:
        # Find the segment object in the original track that matches the earliest selected name
        first_seg = next(
            (s for s in track.segments if str(s.name).strip("'").strip() in selected_segments),
            list(track.segments)[0]
        )
    
    sequence.current_time = first_seg.record_in
    sequence.insert(tmp_clip, destination_track=new_track)
    flame.messages.show_in_console("Done", 'info')
    flame.delete(tmp_reel, confirm=False)

    if missing_graded_shots:
        missing_str = "\n".join(missing_graded_shots)
        flame.messages.show_in_dialog(
            title="Missing graded movs",
            message=f"Missing graded shots:\n{missing_str}",
            type="info",
            buttons=["Ok"]
        )


def _export(first_export=False, comp_update=False, edl=False, clips=None, project_path=None, export_paths=None, overwrite=None):
    """
    Handles all three export types from the UI:

    first_export:  exports L1/layer EXR image sequences to colour_from_flame
    comp_update:   exports comp update EXR sequences to colour_from_flame
    edl:           duplicates the sequence, isolates the selected track,
                   exports EDL + REF QuickTime to colour_from_flame_edl

    overwrite:     list of shot names selected by the user to re-export.
                   When provided, their paths are appended to export_paths
                   and ExportHook silences Flame's built-in overwrite dialog.

    All exported paths are appended to _clipboard_text and copied to the
    system clipboard after each block so the user can paste paths immediately.
    """
    global _clipboard_text

    if first_export and clips and (export_paths or overwrite):
        
        # Build and append paths for shots the user explicitly selected to overwrite
        if overwrite:
            overwrite_shot_names = ['_'.join(s.split('_')[:2]) for s in overwrite]
            colour_root = os.path.join(project_path, CONFIG['colour_from_flame'])
            for seg in clips[:]:
                name      = str(seg.name).strip("'").strip()
                shot_name = '_'.join(name.split('_')[:2])
                if shot_name in overwrite_shot_names:
                    seq       = shot_name.split('_')[0]
                    shot_dir  = os.path.join(colour_root, seq, shot_name)
                    file_name = f'{shot_name}{CONFIG["first_export_suffix"]}'
                    export_paths.append(os.path.join(shot_dir, file_name))

        
        flame.messages.show_in_console(f"Export paths: {export_paths}")
        exporter    = flame.PyExporter()
        preset_path = os.path.join(
            exporter.get_presets_dir(
                flame.PyExporter.PresetVisibility.User,
                flame.PyExporter.PresetType.Image_Sequence
            ),
            CONFIG['first_export_preset'] + '.xml'
        )

        output_dir = os.path.join(project_path,CONFIG['colour_from_flame'])
        exporter.foreground = False
        exporter.keep_timeline_fx_renders = False

        desktop  = flame.project.current_project.current_workspace.desktop
        tmp_reel = desktop.reel_groups[0].create_reel("tmp_export")
        exported = []

        # ExportHook silences Flame's overwrite dialog — only used when overwrite is set
        hook = ExportHook() if overwrite else None
        
        try:
            for seg in clips:
                name      = str(seg.name).strip("'").strip()
                shot_name = '_'.join(name.split('_')[:2])
                # Skip segments that don't have a matching export path
                matching_path = next((p for p in export_paths if shot_name in p), None)
                if not matching_path:
                    flame.messages.show_in_console(f"Skipping {shot_name} — already exists", 'info')
                    continue

                tmp_clip   = seg.match(tmp_reel, include_timeline_fx=False)
                output_dir = os.path.dirname(os.path.dirname(matching_path))
                os.makedirs(output_dir, exist_ok=True)
                exporter.export(tmp_clip, preset_path, output_dir,hooks=hook)
                exported.append(output_dir + "/" + shot_name)
                _clipboard_text.append(output_dir + "/" + shot_name)
            
            
            QtWidgets.QApplication.clipboard().setText("\n".join(_clipboard_text))
            if exported:
                flame.messages.show_in_dialog(
                    title="SHOT(s) EXPORT",
                    message="Shots for grade exporting in the background.\nPaths:\n" + "\n".join(exported),
                    type="info",
                    buttons=["Ok"]
                )
        except Exception as e:
            flame.messages.show_in_console(f"Export error: {str(e)}", 'info')
        finally:
            flame.delete(tmp_reel, confirm=False)



    if comp_update and clips and (export_paths or overwrite):
        
        if overwrite:
            overwrite_shot_names = ['_'.join(s.split('_')[:2]) for s in overwrite]
            colour_root = os.path.join(project_path, CONFIG['colour_from_flame'])
            for seg in clips[:]:
                name      = str(seg.name).strip("'").strip()
                shot_name = '_'.join(name.split('_')[:2])
                if shot_name in overwrite_shot_names:
                    seq      = shot_name.split('_')[0]
                    shot_dir = os.path.join(colour_root, seq, shot_name)
                    export_paths.append(os.path.join(shot_dir, name))
        
        flame.messages.show_in_console(f"Export paths: {export_paths}")
        exporter    = flame.PyExporter()
        preset_path = os.path.join(
            exporter.get_presets_dir(
                flame.PyExporter.PresetVisibility.User,
                flame.PyExporter.PresetType.Image_Sequence
            ),
            CONFIG['comp_export_preset'] + '.xml'
        )

        exporter.foreground               = False
        exporter.keep_timeline_fx_renders = False

        desktop  = flame.project.current_project.current_workspace.desktop
        tmp_reel = desktop.reel_groups[0].create_reel("tmp_export")
        exported = []
        hook = ExportHook() if overwrite else None

        try:
            for seg in clips:
                name      = str(seg.name).strip("'").strip()
                shot_name = '_'.join(name.split('_')[:2])

                matching_path = next((p for p in export_paths if shot_name in p), None)
                
                if not matching_path:
                    flame.messages.show_in_console(f"Skipping {shot_name} — already exists", 'info')
                    continue

                tmp_clip   = seg.match(tmp_reel, include_timeline_fx=False)
                output_dir = os.path.dirname(os.path.dirname(matching_path))
                os.makedirs(output_dir, exist_ok=True)
                exporter.export(tmp_clip, preset_path, output_dir,hooks=hook)
                exported.append(output_dir + "/" + shot_name)
                _clipboard_text.append(output_dir + "/" + shot_name)

            QtWidgets.QApplication.clipboard().setText("\n".join(_clipboard_text))
            if exported:
                flame.messages.show_in_dialog(
                    title="SHOT(s) EXPORT",
                    message="Shots for grade exporting in the background.\nPaths:\n" + "\n".join(exported),
                    type="info",
                    buttons=["Ok"]
                )

        except Exception as e:
            flame.messages.show_in_console(f"Export error: {str(e)}", 'info')
        finally:
            flame.delete(tmp_reel, confirm=False)



    if edl and clips:
        exporter    = flame.PyExporter()
        preset_path = os.path.join(
            exporter.get_presets_dir(
                flame.PyExporter.PresetVisibility.User,
                flame.PyExporter.PresetType.Sequence_Publish
            ),
            CONFIG['edl_export_preset'] + '.xml'
        )
        
        output_dir = os.path.join(project_path, CONFIG['colour_from_flame_edl'])
        os.makedirs(output_dir, exist_ok=True)

        try:
            for track, seq_input in clips:
                flame.messages.show_in_console(f"Track attributes: {track.attributes}", 'info')
                flame.messages.show_in_console(f"Track dir: {[x for x in dir(track) if not x.startswith('_')]}", 'info')
                
                new_name = seq_input.text().strip()
                sequence = track.parent.parent
                tmp_seq  = flame.duplicate(sequence)
                tmp_seq.name = new_name

                # Keep only the selected track
                original_version = track.parent
                tmp_version      = tmp_seq.versions[0]
                track_pos        = list(original_version.tracks).index(track)

                target_track      = list(tmp_version.tracks)[track_pos]
                original_name     = str(target_track.name).strip("'").strip()
                target_track.name = '__keep__'

                for t in list(tmp_version.tracks):
                    if str(t.name).strip("'").strip() != '__keep__':
                        flame.delete(t, confirm=False)

                target_track.name = original_name
                
                exporter.foreground               = False
                exporter.export(tmp_seq, preset_path, output_dir)

                # Export REF mov
                mov_exporter    = flame.PyExporter()
                mov_preset_path = os.path.join(
                    mov_exporter.get_presets_dir(
                        flame.PyExporter.PresetVisibility.User,
                        flame.PyExporter.PresetType.Movie
                    ),
                    CONFIG['edl_ref_export_preset'] + '.xml'
                )

                mov_exporter.export(tmp_seq, mov_preset_path, output_dir)
                _clipboard_text.append(output_dir + "/" + new_name)


                
                QtWidgets.QApplication.clipboard().setText("\n".join(_clipboard_text))
                flame.messages.show_in_dialog(
                title="EDL EXPORT",
                message=f"Exported {new_name} EDL",
                type="info",
                buttons=["Ok"]
            )

                flame.delete(tmp_seq, confirm=False)

        except Exception as e:
            flame.messages.show_in_console(f"EDL export error: {str(e)}", 'info')

def create_export_to_grade_UI(project_path, first_export_paths, comp_updates_paths, existing, edl_to_create, first_export_item, comp_updates_item, open_clips_paths, layers_item):
    """
    Main export dialog. Shows a summary of what will be exported and provides
    per-track checkboxes for EDL, comp track and graded track creation.

    Layout:
        - Project path label
        - New shots text area (first_export_paths)
        - Comp updates text area (comp_updates_paths)
        - Existing shots list (selectable for overwrite)
        - Export checkboxes: Grade_v0, comp updates, overwrite
        - Scrollable track section: one group per track with EDL/comp/grade checkboxes
          and coloured segment pills
        - Ok / Cancel buttons

    On accept:
        Clears clipboard, then runs selected operations in order:
        export v0 → export comp → export EDL → create comp track → create graded track
    """
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle("Export to Grade")
    dialog.setMinimumSize(900, 600)

    main_layout = QtWidgets.QVBoxLayout(dialog)

    #PROJECT_LABEL
    project = check_project()
    if project:
        job_button = QtWidgets.QPushButton(project.PRJ_PATH.split('/')[-1])
        job_button.setCheckable(True)
        job_button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        job_button.setStyleSheet("""
        QPushButton {
            background-color: #cc0033;
            color: white;
            border-radius: 8px;
            font-size: 11px;
            font-weight: bold;
            border-bottom: 3px solid #880022;
            padding: 4px 12px;
            outline: none;
        }
        QPushButton:checked {
            background-color: #444444;
            color: #aaaaaa;
            border-bottom: 1px solid #222222;
            outline: none;
        }
        """)
        main_layout.addWidget(job_button)
    # --- New shots ---
    label_first_exports = QtWidgets.QLabel(f"Exporting {len(first_export_paths)} new shot(s) to:")
    main_layout.addWidget(label_first_exports)

    text_first_exports = QtWidgets.QTextEdit()
    text_first_exports.setReadOnly(True)
    text_first_exports.setMaximumHeight(80)
    text_first_exports.setText("\n".join(first_export_paths) if first_export_paths else "None")
    main_layout.addWidget(text_first_exports)

    # --- Comp updates ---
    label_comp_updates = QtWidgets.QLabel(f"Exporting {len(comp_updates_paths)} comp update(s) to:")
    main_layout.addWidget(label_comp_updates)

    text_comp_updates = QtWidgets.QTextEdit()
    text_comp_updates.setReadOnly(True)
    text_comp_updates.setMaximumHeight(80)
    text_comp_updates.setText("\n".join(comp_updates_paths) if comp_updates_paths else "None")
    main_layout.addWidget(text_comp_updates)

    # --- Existing (skipped) ---
    label_existing = QtWidgets.QLabel(f"Skipped {len(existing)} already existing shot(s):")
    label_existing.setStyleSheet("color: orange;")
    main_layout.addWidget(label_existing)

    list_existing = QtWidgets.QListWidget()
    list_existing.setMaximumHeight(80)
    list_existing.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
    for shot in existing:
        list_existing.addItem(shot)
    main_layout.addWidget(list_existing)

    # --- EDL section — one block per track ---
    edl_label = QtWidgets.QLabel("Export:")
    edl_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
    main_layout.addWidget(edl_label)

    # Export v000
    export_v0_checkbox = QtWidgets.QCheckBox("Export Grade_v0")
    export_v0_checkbox.setChecked(True)
    export_v0_checkbox.stateChanged.connect(
        lambda state, w=text_first_exports, l=label_first_exports: (
            w.setText("\n".join(first_export_paths) if state else "None"),
            l.setText(f"Exporting {len(first_export_paths)} new shot(s) to:" if state else "Exporting no new shot(s)")
        )
    )
    
    if not len(first_export_paths) == 0:
        main_layout.addWidget(export_v0_checkbox)
    else:
        export_v0_checkbox.setChecked(False)
    

    # Export comps
    export_comp_checkbox = QtWidgets.QCheckBox("Export comp updates")
    export_comp_checkbox.setChecked(True)
    export_comp_checkbox.stateChanged.connect(
        lambda state, w=text_comp_updates, l=label_comp_updates: (
            w.setText("\n".join(comp_updates_paths) if state else "None"),
            l.setText(f"Exporting {len(comp_updates_paths)} comp update(s) to:" if state else "Exporting 0 comp update(s)")
        )
    )
    
    if not len(comp_updates_paths) == 0:
        main_layout.addWidget(export_comp_checkbox)

    overwrite_checkbox = QtWidgets.QCheckBox("Overwrite existing shots")
    overwrite_checkbox.setChecked(False)

    if existing:
        main_layout.addWidget(overwrite_checkbox)
    
    # --- Scrollable track section ---
    # One group per track, each showing: EDL/comp/grade checkboxes and segment colour pills
    
    scroll_area = QtWidgets.QScrollArea()
    scroll_area.setWidgetResizable(True)
    scroll_widget = QtWidgets.QWidget()
    scroll_layout = QtWidgets.QVBoxLayout(scroll_widget)


    edl_tracks_to_export   = []  # list of (track, seq_input) tuples for EDL export
    graded_track_to_create = []  # list of tracks for graded track creation
    comp_track_to_create   = []  # list of tracks for comp track creation
    overwrite_shots = []
    track_selected_segs = {}

    for sequence, tracks in edl_to_create.items():
        seq_name = str(sequence.name).strip("'").strip()



        # Detect track type from non-black segment names
        # is_first: standard L1 shot — gets all three checkboxes ticked by default
        # is_layer: L2+ layer — only EDL ticked
        # is_comp:  comp update — only EDL ticked
        
        for track in tracks:

            # Detect track type from segments
            # Default EDL name: seq_name for L1, seq_name_L2 for layers, seq_name_comp for comp
            segs_names = [str(s.name).strip("'").strip() for s in track.segments 
                         if str(s.colour).strip('()').split(',')[0].strip() != '0.0']
            is_layer = any(re.match(CONFIG['pattern_layer'], n) for n in segs_names)
            is_comp  = any(re.match(CONFIG['pattern_comp_update'], n) for n in segs_names)
            is_first = not is_layer and not is_comp

            if is_layer:
                layer_suffix = segs_names[0].split('_')[2] if segs_names else ''
                default_name = f'{seq_name}_{layer_suffix}'
            elif is_comp:
                default_name = f'{seq_name}_comp'
            else:
                default_name = seq_name


            # Editable name field — pre-filled with default_name, user can rename before export
            group        = QtWidgets.QGroupBox()
            group_layout = QtWidgets.QVBoxLayout(group)
            seq_layout   = QtWidgets.QHBoxLayout()

            seq_input = QtWidgets.QLineEdit(default_name)
            seq_input.setFixedWidth(300)
            seq_layout.addWidget(QtWidgets.QLabel("Name:"))
            seq_layout.addWidget(seq_input)

            export_EDL_checkbox      = QtWidgets.QCheckBox("Export EDL")
            create_comp_track_cb     = QtWidgets.QCheckBox("Create comp track")
            create_graded_track_cb   = QtWidgets.QCheckBox("Create graded track")


            # Auto-tick based on track type and whether new shots are being exported
            if first_export_paths:
                export_EDL_checkbox.setChecked(True)
                edl_tracks_to_export.append((track, seq_input))
                if is_first:
                    create_comp_track_cb.setChecked(True)
                    comp_track_to_create.append(track)
                    create_graded_track_cb.setChecked(True)
                    graded_track_to_create.append(track)
            
            # Checkbox signals update the relevant lists on toggle
            export_EDL_checkbox.stateChanged.connect(
                lambda state, t=track, s=seq_input:
                    edl_tracks_to_export.append((t, s)) if state else edl_tracks_to_export.remove((t, s))
            )
            create_comp_track_cb.stateChanged.connect(
                lambda state, t=track:
                    comp_track_to_create.append(t) if state else comp_track_to_create.remove(t)
            )
            create_graded_track_cb.stateChanged.connect(
                lambda state, t=track:
                    graded_track_to_create.append(t) if state else graded_track_to_create.remove(t)
            )

            seq_layout.addWidget(export_EDL_checkbox)
            seq_layout.addWidget(create_comp_track_cb)
            seq_layout.addWidget(create_graded_track_cb)
            seq_layout.addStretch()
            group_layout.addLayout(seq_layout)

            # Segment pills — coloured labels showing each non-black segment in the track
            # Colour is derived from the segment's timeline colour (RGB 0-1 → hex)
            selected_segs = set()
            track_selected_segs[track] = selected_segs
            row_layout      = QtWidgets.QHBoxLayout()
            seg_row        = QtWidgets.QWidget()
            seg_row_layout = QtWidgets.QHBoxLayout(seg_row)
            seg_row_layout.setContentsMargins(0, 0, 0, 0)
            seg_row_layout.setSpacing(3)

            for seg in track.segments:
                seg_name_str = str(seg.name).strip("'").strip()

                colour_str = str(seg.colour).strip('()')
                r, g, b    = [float(x.strip()) for x in colour_str.split(',')]
                
                if not seg_name_str:
                    continue  # skip truly empty segments with no name
                # Use segment colour if set, fall back to neutral grey for uncoloured segments
                if r == 0.0 and g == 0.0 and b == 0.0:
                    bg_color = '#888888'
                else:
                    bg_color = f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'

                lbl = QtWidgets.QLabel(seg_name_str)
                lbl.setFixedHeight(26)
                lbl.setAlignment(QtCore.Qt.AlignCenter)
                lbl.setContentsMargins(8, 0, 8, 0)
                lbl.setStyleSheet(f"""
                    QLabel {{
                        background-color: {bg_color};
                        color: white;
                        border-radius: 3px;
                        font-size: 11px;
                        border: 2px solid transparent;
                    }}
                """)
                lbl.setCursor(QtCore.Qt.PointingHandCursor)

                def make_toggle(label, name, bg, segs_set, overwrite_shots_ref, overwrite_cb):
                    def toggle(event):
                        shot_name = '_'.join(name.split('_')[:2])
                        if name in segs_set:
                            segs_set.discard(name)
                            if overwrite_cb.isChecked() and shot_name in overwrite_shots_ref:
                                overwrite_shots_ref.remove(shot_name)
                            label.setStyleSheet(f"""
                                QLabel {{
                                    background-color: {bg};
                                    color: white;
                                    border-radius: 3px;
                                    font-size: 11px;
                                    border: 2px solid transparent;
                                }}
                            """)
                        else:
                            segs_set.add(name)
                            if overwrite_cb.isChecked():
                                if shot_name not in overwrite_shots_ref:
                                    overwrite_shots_ref.append(shot_name)
                                border_color = '#00b400'
                            else:
                                border_color = '#4da6ff'
                            label.setStyleSheet(f"""
                                QLabel {{
                                    background-color: {bg};
                                    color: white;
                                    border-radius: 3px;
                                    font-size: 11px;
                                    border: 2px solid {border_color};
                                }}
                            """)
                    return toggle

                lbl.mousePressEvent = make_toggle(
                    lbl, seg_name_str, bg_color, selected_segs,
                    overwrite_shots, overwrite_checkbox
                )
                seg_row_layout.addWidget(lbl)

            seg_row_layout.addStretch()
            row_layout.addWidget(seg_row)
            row_layout.addStretch()
            group_layout.addLayout(row_layout)
            scroll_layout.addWidget(group)

    scroll_area.setWidget(scroll_widget)
    main_layout.addWidget(scroll_area)

    # --- Buttons ---
    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    main_layout.addWidget(buttons)

    
    
    # --- Overwrite toggle ---
    # When checked: informs user, enables multi-selection on list_existing,
    # selected items turn green and are stored in overwrite_shots
    # When unchecked: clears selection and resets item backgrounds
    def on_overwrite_toggled(state):
        if not state:
            overwrite_shots.clear()
            # Deselect all pills and reset borders
            for track, pill_labels in track_pill_labels.items():
                segs_set = track_selected_segs.get(track, set())
                segs_set.clear()
                for name, lbl in pill_labels.items():
                    colour_str = str(next(s for s in track.segments 
                                   if str(s.name).strip("'").strip() == name).colour).strip('()')
                    r, g, b = [float(x.strip()) for x in colour_str.split(',')]
                    bg = '#888888' if r == 0.0 and g == 0.0 and b == 0.0 else f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'
                    lbl.setStyleSheet(f"""
                        QLabel {{
                            background-color: {bg};
                            color: white;
                            border-radius: 3px;
                            font-size: 11px;
                            border: 2px solid transparent;
                        }}
                    """)

    overwrite_checkbox.stateChanged.connect(on_overwrite_toggled)

    # --- Accepted block ---
    # Clipboard is cleared once here so all subsequent _export calls
    # accumulate into a single clipboard entry
    
    if dialog.exec() == QtWidgets.QDialog.Accepted:

        _clipboard_text.clear()
        QtWidgets.QApplication.clipboard().clear()
        
        if export_v0_checkbox.isChecked():
            _export(
                first_export=True,
                clips=first_export_item + layers_item,
                project_path=project_path,
                export_paths=first_export_paths,
                overwrite=overwrite_shots
            )
            if open_clips_paths:
                create_graded_open_clip(project_path, open_clips_paths)
            create_grade_first_version(first_export_item + layers_item)
        
        if export_comp_checkbox.isChecked():
            _export(
                comp_update=True,
                clips=comp_updates_item,
                project_path=project_path,
                export_paths=comp_updates_paths,
                overwrite=overwrite_shots
            )
            if open_clips_paths:
                create_graded_open_clip(project_path, open_clips_paths)
        
        if edl_tracks_to_export:
            _export(
                edl=True,
                clips=edl_tracks_to_export,
                project_path=project_path
            )

        if comp_track_to_create:
            for track in comp_track_to_create:
                segs = []
                for seg in track.segments:
                    name = str(seg.name).strip("'").strip()
                    colour_str = str(seg.colour).strip('()')
                    r, g, b = [float(x.strip()) for x in colour_str.split(',')]
                    if name and not (r == 0.0 and g == 0.0 and b == 0.0):
                        segs.append(seg)
                
                create_comp_first_version(segs)
                create_comp_open_clip(segs)
                selected = track_selected_segs.get(track)
                create_comp_track(track, selected_segments=selected if selected else None)

        if graded_track_to_create:
            if open_clips_paths:
                create_graded_open_clip(project_path, open_clips_paths)
            create_grade_first_version(first_export_item + layers_item)
            for track in graded_track_to_create:
                selected = track_selected_segs.get(track)
                create_graded_track(track, selected_segments=selected if selected else None)
    else:
        flame.messages.show_in_console("Export cancelled", 'info')
        
def export_to_grade(selection):
    """
    Entry point called by Flame when the user triggers Pipeline > Export to Grade.
    
    Sorts selected segments into four buckets by name pattern:
        first_export:  L1 shots  (e.g. SAC_010_L1)
        layers:        L2+ shots (e.g. SAC_010_L2)
        comp_updates:  comp render versions (e.g. SAC_010_comp_render_main_v002)
        wrong_naming:  anything that doesn't match — user is shown an error and blocked

    Then builds colour paths, collects tracks, builds the EDL dict
    and opens the export UI.
    """
    
    project = check_project()
    if not project:
        return

    wrong_naming         = []
    first_export         = []
    comp_updates         = []
    layers               = []
    comp_no_version      = []
    

    wrong_naming_item    = []
    first_export_item    = []
    comp_updates_item    = []
    layers_item          = []
    comp_no_version_item = []
    

    for item in selection:
        name = str(item.name).strip("'").strip()

        if not name:
            continue

        if re.match(CONFIG['pattern_first_export'], name):
            first_export.append(name)
            first_export_item.append(item)

        elif re.match(CONFIG['pattern_layer'], name):
            layers.append(name)
            layers_item.append(item)

        elif re.match(CONFIG['pattern_comp_update'], name):
            comp_updates.append(name)
            comp_updates_item.append(item)

        elif re.match(CONFIG['pattern_comp_no_version'], name):
            comp_no_version.append(name)
            comp_no_version_item.append(item)

        else:
            wrong_naming.append(name)
            wrong_naming_item.append(item)

    if comp_no_version:
        no_version_str = "\n".join(comp_no_version)
        dialog = flame.messages.show_in_dialog(
            title="Missing Version Number",
            message=f"The following segments are missing a version number:\n\n{no_version_str}\n\n"
                    f"They will be renamed to add the current v number.\n"
                    f"After export completes, please rerun Export to Grade.",
            type="question",
            buttons=["Rename and Export"],
            cancel_button="Cancel"
        )

        # Rename segments in Flame and add to comp_updates
        for seg in comp_no_version_item:
            seg.name = str(seg.name).strip("'").strip() + '_v<source version>'
            seg.__setattr__('dynamic_name', True)
        return
    
    # Block the user if any segments don't match the expected naming convention
    if wrong_naming:
        QtWidgets.QMessageBox.critical(
            None,
            "Rename Shot",
            "Please rename the following clips:\n\n\n" + "\n".join(wrong_naming) + "\n\n\nExpected format: SEQ_###_L#" + "\n\n\nExpected format: SEQ_SHOTNUMBER_comp_render_main_version"
        )
        return
    
    all_segments = first_export_item + comp_updates_item + layers_item
    first_export_paths, comp_updates_paths, open_clips_paths, existing = build_color_path(project.PRJ_PATH, first_export, comp_updates, layers)
    tracks        = get_tracks(project.PRJ_PATH, all_segments)
    edl_to_create = create_EDL(tracks)
    create_export_to_grade_UI(project.PRJ_PATH,first_export_paths,comp_updates_paths,existing,edl_to_create,first_export_item,comp_updates_item,open_clips_paths,layers_item)



def create_visional_shots(segments):
    """
    Checks if the shots have been exported to server. If not it uses the Visional 
    pipeline's export_clip hook to publish the plate from Flame to the shots directory 
    first.

    Two paths:
        - src_folder exists on disk: queued directly for copy in to_copy
        - src_folder missing: segment added to needs_export for Visional export first,
          then re-checked after export completes
    """
    project = check_project()
    if not project:
        return

    project_name  = project.PRJ_PATH
    project_alias = project.alias
    shots_dir     = os.path.join(project_name, 'shots')
    graded_root   = os.path.join(project_name, CONFIG['colour_for_flame'], 'shots')

    to_copy          = {}
    needs_export     = []  # segments that need Export Clip first

    for segment in segments:
        name      = str(segment.name).strip("'").strip()
        seq       = name.split('_')[0]
        shot_name = '_'.join(name.split('_')[:2])

        src_folder = os.path.join(
            shots_dir, seq, shot_name,
            'bg', 'data', 'plate', 'L1',
            f'{project_alias}_shots_{shot_name}_bg_plate_L1_v001'
        )

        dst_folder    = os.path.join(graded_root, seq, shot_name)
        existing_files = [f for f in os.listdir(dst_folder) if f.startswith(f'{shot_name}_comp_render_main_v000_graded')] if os.path.exists(dst_folder) else []

        if existing_files:
            flame.messages.show_in_console(f"v000_graded already exists for {shot_name} — skipping", 'info')
            continue

        if not os.path.exists(src_folder):
            needs_export.append(segment)
        else:
            to_copy[src_folder] = (dst_folder, shot_name)

    if needs_export:
        try:
            sys.path.insert(0, '/studio/adv/visional/linux/VisionalPipeline0.9.145-alpha.1/plugins/apps/flame/hooks')
            from vp_custom_actions_hook import export_clip, get_sequence_data

            desktop  = flame.project.current_project.current_workspace.desktop
            tmp_reel = desktop.reel_groups[0].create_reel("tmp_grade_export")

            clips         = []
            sequence_data = None

            for seg in needs_export:
                try:
                    clip = seg.match(tmp_reel, include_timeline_fx=False)
                    if clip:
                        if sequence_data is None:
                            sequence_data = get_sequence_data(seg.parent.parent.parent)
                        clips.append(clip)
                except Exception as e:
                    flame.messages.show_in_console(f"match error for {seg.name}: {str(e)}", 'info')

            if clips:
                export_clip(clips, sequence_data=sequence_data)

            flame.delete(tmp_reel, confirm=False)

        except Exception as e:
            flame.messages.show_in_console(f"Visional export error: {str(e)}", 'info')

def create_comp_first_version(segments):
    """
    Creates comp_render_main_v000 by copying and renaming EXR files from bg_plate_L1.
    This gives the comp department a v000 placeholder to work from before any renders exist.

    File rename on copy:
        _bg_    → _comp_
        _plate_ → _render_
        _L1_    → _main_
        _v001   → _v000

    Two paths:
        - src_folder exists: queued for copy via CopyWorker
        - src_folder missing: user is prompted to export via Visional pipeline first

    If the user declines to export missing plates, the function returns early.
    Skips shots where v000 already exists and is non-empty.
    Progress is shown via QDialog + QProgressBar + CopyWorker QThread.
    """

    project = check_project()
    if not project:
        return

    project_name  = project.PRJ_PATH
    project_alias = project.alias
    shots_dir     = os.path.join(project_name, 'shots')
    
    clips_to_server = []
    to_copy = {}

    for segment in segments:
        name      = str(segment.name).strip("'").strip()
        seq       = name.split('_')[0]
        shot_name = '_'.join(name.split('_')[:2])

        src_folder = os.path.join(
            shots_dir, seq, shot_name,
            'bg', 'data', 'plate', 'L1',
            f'{project_alias}_shots_{shot_name}_bg_plate_L1_v001'
        )

        dst_folder = os.path.join(
            shots_dir, seq, shot_name,
            'comp', 'data', 'render', 'main',
            f'{project_alias}_shots_{shot_name}_comp_render_main_v000'
        )
        
        if os.path.exists(dst_folder) and os.listdir(dst_folder):
            flame.messages.show_in_console(f"v000 already exists for {shot_name} — skipping", 'info')
            continue

        if not os.path.exists(src_folder):
            clips_to_server.append(segment)
            continue

        to_copy[src_folder] = dst_folder

    # Re-check paths after Visional export — add any now-available shots to to_copy
    if clips_to_server:
        missing_names = [str(s.name).strip("'").strip() for s in clips_to_server]
        missing_str   = "\n".join(missing_names)
        dialog = flame.messages.show_in_dialog(
            title="Missing plates",
            message=f"bg_plate_L1 not found for:\n{missing_str}\n\nDo you want to export them now?",
            type="question",
            buttons=["Export Clip To Server"],
            cancel_button="Skip"
        )
        if dialog == "Export Clip To Server":
            create_visional_shots(clips_to_server)
            
            # ADD JUST EXPORTED SHOTS.

            for segment in clips_to_server:
                name      = str(segment.name).strip("'").strip()
                seq       = name.split('_')[0]
                shot_name = '_'.join(name.split('_')[:2])
                src_folder = os.path.join(
                    shots_dir, seq, shot_name,
                    'bg', 'data', 'plate', 'L1',
                    f'{project_alias}_shots_{shot_name}_bg_plate_L1_v001'
                )
                dst_folder = os.path.join(
                    shots_dir, seq, shot_name,
                    'comp', 'data', 'render', 'main',
                    f'{project_alias}_shots_{shot_name}_comp_render_main_v000'
                )
                if os.path.exists(src_folder):
                    to_copy[src_folder] = dst_folder
                else:
                    flame.messages.show_in_console(f"{shot_name} still not found after export — skipping", 'info')
        
        else:
            dialog = flame.messages.show_in_dialog(
                title="Missing comp v000",
                message=f"You need to export clips to server to create a comp track",
                type="info",
                cancel_button="Ok"
            )
            return
    
    if not to_copy:
        return

    errors = []


    # Progress dialog with QThread — timer gives Qt 200ms to paint before copy starts
    progress_dialog = QtWidgets.QDialog()
    progress_dialog.setWindowTitle("Create Comp Version 0")
    progress_dialog.setMinimumWidth(500)
    progress_dialog.setWindowModality(QtCore.Qt.WindowModal)
    layout = QtWidgets.QVBoxLayout(progress_dialog)
    label  = QtWidgets.QLabel("Starting...")
    bar    = QtWidgets.QProgressBar()
    bar.setMinimum(0)
    bar.setMaximum(len(to_copy))
    bar.setValue(0)
    layout.addWidget(label)
    layout.addWidget(bar)
    progress_dialog.show()
    QtWidgets.QApplication.processEvents()

    worker = CopyWorker(to_copy)
    worker.progress.connect(lambda count, msg: (bar.setValue(count), label.setText(msg)))
    worker.error.connect(lambda e: errors.append(e))
    worker.finished.connect(progress_dialog.close)

    QtCore.QTimer.singleShot(200, worker.start)

    while not worker.isFinished():
        QtWidgets.QApplication.processEvents()

    if errors:
        flame.messages.show_in_console("\n".join(errors), 'info')
    else:
        flame.messages.show_in_console(f"Created v000 for {len(to_copy)} shot(s).", 'info')

def create_comp_open_clip(segments):
        """
        Creates open clip XML files for comp shots in the sequence pipeline directory.
        The ScanPattern points to the comp EXR render folder so Flame can discover
        new comp versions as they are delivered.

        Output path: shots/SEQ/SEQ_sequence/conform/work/flame/pipeline/SHOT_comp_render_main.clip
        ScanPattern:  shots/SEQ/SHOT/comp/data/render/main/
                    alias_shots_SHOT_comp_render_main_v{version}/
                    alias_shots_SHOT_comp_render_main_v{version}_{track}.{frame}.exr

        Skips clips that already have an existing .clip file.
        """
        project = check_project()
        if not project:
            return
        
        project_name = project.PRJ_PATH
        project_alias = project.alias

        shots_dir = os.path.join(project_name, 'shots')
        created       =  []

        flame.messages.show_in_console(f"Selected: {project_alias}", 'info')
        
        for segment in segments:
            name      = str(segment.name).strip("'").strip()
            seq       = name.split('_')[0]   # e.g. TRT
            shot_name = '_'.join(name.split('_')[:2])  # e.g. TRT_030
            clip_name = f'{shot_name}_comp_render_main'

            open_clip_dir = os.path.join(
                shots_dir,
                seq,
                f'{seq}_sequence',
                'conform',
                'work',
                'flame',
                'pipeline'  
            )

            os.makedirs(open_clip_dir, exist_ok=True)

            open_clip_path = os.path.join(open_clip_dir, f'{clip_name}.clip')

            if os.path.exists(open_clip_path):
                flame.messages.show_in_console(f"Skipped (already exists): {clip_name}.clip", 'info')
                continue

            scan_pattern = os.path.join(
                shots_dir,
                seq,
                shot_name,
                "comp",
                "data",
                "render",
                "main",
                f'{project_alias}_shots_{shot_name}_comp_render_main_v{{version}}',
                f'{project_alias}_shots_{shot_name}_comp_render_main_v{{version}}_{{track}}.{{frame}}.exr'
            )
            xml = f'''<?xml version="1.0"?>
    <clip type="clip" version="6">
        <handler>
            <name>MIO Clip</name>
            <version>2</version>
            <options type="dict">
                <ScanPattern type="string">{scan_pattern}</ScanPattern>
            </options>
        </handler>
    </clip>'''

            with open(open_clip_path, 'w') as f:
                f.write(xml)

            flame.messages.show_in_console(f"Created openclip: {open_clip_path}", 'info')
            created.append(f'{clip_name}.clip')

def create_comp_track(track,selected_segments=None):
    """
    Creates a new comp timeline track by copying the selected track to a temp reel,
    replacing each segment's media with the corresponding comp open clip,
    then inserting the modified clip back into the sequence on a new track.

    For each segment:
        - Looks up the open clip at shots/SEQ/SEQ_sequence/conform/work/flame/pipeline/
        - Checks comp_render_main_v000 directory exists and is non-empty
        - If v000 exists: replaces media via smart_replace_media, renames segment
          to include '_v<source version>' and sets blue colour (0.094, 0.224, 0.361)
        - If v000 missing: adds to missing_L1, skips

    If missing shots are found after the loop, user is offered to create v000 on the spot.
    """
    _warn_bfx()

    project = check_project()
    if not project:
        return

    project_name = project.PRJ_PATH
    project_alias = project.alias
    version      = track.parent
    sequence     = version.parent

    desktop  = flame.project.current_project.current_workspace.desktop
    tmp_reel = desktop.reel_groups[0].create_reel("tmp_comp")
    tmp_clip = track.copy_to_media_panel(tmp_reel)


    progress = QtWidgets.QProgressDialog("Generating comp timeline...", None, 0, len(list(track.segments)))
    progress.setWindowTitle("Create Comp Track")
    progress.setMinimumWidth(500)
    progress.setWindowModality(QtCore.Qt.WindowModal)
    progress.show()
    QtWidgets.QApplication.processEvents()

    missing_L1 = []


    count = 0
    for v in tmp_clip.versions:
        for t in v.tracks:
            for seg in t.segments:
                name      = str(seg.name).strip("'").strip()
                seq       = name.split('_')[0]
                shot_name = '_'.join(name.split('_')[:2])
                clip_name = f'{shot_name}_comp_render_main'


                # If specific segments were selected, delete unselected ones from tmp_clip
                if selected_segments and name not in selected_segments:
                    flame.delete(seg, confirm=False)
                    count += 1
                    progress.setValue(count)
                    QtWidgets.QApplication.processEvents()
                    continue

                open_clip_path = os.path.join(
                    project_name, 'shots',
                    seq, f'{seq}_sequence',
                    'conform', 'work', 'flame', 'pipeline',
                    f'{clip_name}.clip'
                )

                if not os.path.exists(open_clip_path):
                    # Open clip missing — check if v000 exists on disk and offer to create it
                    comp_v000_dir = os.path.join(
                        project_name, 'shots', seq, shot_name,
                        'comp', 'data', 'render', 'main',
                        f'{project_alias}_shots_{shot_name}_comp_render_main_v000'
                    )
                    if os.path.exists(comp_v000_dir) and os.listdir(comp_v000_dir):    
                        create_comp_open_clip([seg])
                    else:
                        flame.messages.show_in_console(f"Missing open clip and no v000: {shot_name}", 'info')
                        missing_L1.append((shot_name, seg))

                else:
                    try:
                        comp_v000_dir = os.path.join(
                            project_name, 'shots', seq, shot_name,
                            'comp', 'data', 'render', 'main',
                            f'{project_alias}_shots_{shot_name}_comp_render_main_v000'
                        )

                        if not os.path.exists(comp_v000_dir) or not os.listdir(comp_v000_dir):
                            flame.messages.show_in_console(f"No comp v000 found for {shot_name} — skipping", 'info')
                            missing_L1.append((shot_name, seg))
                            count += 1
                            progress.setValue(count)
                            QtWidgets.QApplication.processEvents()
                            continue

                        # v000 exists — do the replace
                        open_clip = flame.import_clips(open_clip_path, tmp_reel)
                        import time
                        time.sleep(0.5)
                        seg.smart_replace_media(open_clip[0])
                        seg.name   = seg.name + '_v<source version>'
                        seg.colour = (0.094, 0.224, 0.361)
                        seg.__setattr__('dynamic_name', True)
                        flame.messages.show_in_console(f"Replaced: {name}", 'info')

                    except Exception as e:
                        flame.messages.show_in_console(f"Error {name}: {str(e)}", 'info')

                count += 1
                progress.setValue(count)
                QtWidgets.QApplication.processEvents()

    progress.close()

    if missing_L1:
        missing_str    = "\n".join([s for s, _ in missing_L1])
        missing_segs   = [seg for _, seg in missing_L1]
        
        dialog = flame.messages.show_in_dialog(
            title="Missing comp v000",
            message=f"No comp v000 found for:\n{missing_str}\n\nDo you want to create it now?",
            type="question",
            buttons=["Create v000"],
            cancel_button="Skip"
        )
        if dialog == "Create v000":
            create_comp_first_version(missing_segs)
    

    if selected_segments:
        first_seg = next(
            (s for s in track.segments if str(s.name).strip("'").strip() in selected_segments),
            list(track.segments)[0]
        )
    else:
        first_seg = list(track.segments)[0]

    sequence.current_time = first_seg.record_in
    new_track = version.create_track(-1)
    sequence.insert(tmp_clip, destination_track=new_track)
    flame.delete(tmp_reel, confirm=False)



def update_open_clip(selection):
    """
    Force-refreshes the open clip media for selected segments.
    Useful when Flame has cached old footage after an overwrite.
    
    Detects clip type from file_path:
        - path contains 'graded' → graded open clip
        - path contains 'comp'   → comp open clip
    
    Imports a fresh copy of the same open clip to a tmp reel,
    replaces the segment's media, then deletes the tmp reel.
    """
    project = check_project()
    if not project:
        return

    desktop  = flame.project.current_project.current_workspace.desktop
    tmp_reel = desktop.reel_groups[0].create_reel("tmp_update_clip")

    failed  = []
    updated = []

    try:
        for seg in selection:
            file_path = str(seg.file_path)
            flame.messages.show_in_console(f"file_path: {file_path}", 'info')

            project_name  = project.PRJ_PATH
            name          = str(seg.name).strip("'").strip()
            shot_name     = '_'.join(name.split('_')[:2])
            seq           = name.split('_')[0]

            # Detect clip type from file_path and build the .clip path
            if 'graded' in file_path:
                clip_path = os.path.join(
                        project_name, CONFIG['open_clip_dir'],
                        f'{shot_name}_comp_render_main.clip'
                    )
            else:
                clip_path = os.path.join(
                        project_name, 'shots',
                        seq, f'{seq}_sequence',
                        'conform', 'work', 'flame', 'pipeline',
                        f'{shot_name}_comp_render_main.clip'
                    )


            if not clip_path or not os.path.exists(clip_path):
                failed.append(str(seg.name).strip("'").strip())
                continue

            fresh_clip = flame.import_clips(clip_path, tmp_reel)
            time.sleep(0.5)
            seg.smart_replace_media(fresh_clip[0])
            
            if 'graded' not in file_path:
                seg.name + '_v<source version>'
                seg.__setattr__('dynamic_name', True)
            else:
                seg.name   = seg.name + '_v<source version>_graded'
                seg.__setattr__('dynamic_name', True)
            
            updated.append(str(seg.name).strip("'").strip())
            flame.messages.show_in_console(f"Updated: {seg.name}", 'info')

    except Exception as e:
        flame.messages.show_in_console(f"Update error: {str(e)}", 'info')
    finally:
        flame.delete(tmp_reel, confirm=False)

    if updated:
        flame.messages.show_in_dialog(
            title="Open Clip Updated",
            message=f"Updated {len(updated)} segment(s):\n" + "\n".join(updated),
            type="info",
            buttons=["Ok"]
        )
    if failed:
        flame.messages.show_in_dialog(
            title="Update Failed",
            message=f"Could not find open clip for:\n" + "\n".join(failed),
            type="info",
            buttons=["Ok"]
        )


def get_timeline_custom_ui_actions():
    """
    Registers the Export to Grade action in Flame's timeline right-click menu
    under the Pipeline group. Requires Flame 2025 or later.
    """
    return [
        {
            'name': 'Pipeline',
            'actions': [
                {
                    'name': 'Export to Grade',
                    'execute': export_to_grade,
                    'minimumVersion': '2025'
                },
                {
                    'name': 'Update Open Clip',
                    'execute': update_open_clip,
                    'minimumVersion': '2025'
                }
            ]
        }
    ]
