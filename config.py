#### JUN 9th 2026

CONFIG = {
    # Export paths
    'colour_from_flame'          :     'colour/media/from_flame/shots',
    'colour_from_flame_edl'      :     'colour/media/from_flame/EDL',
    'colour_for_flame'           :     'colour/media/for_flame/graded/shots',
    'open_clip_dir'              :     'colour/media/for_flame/graded/opClips',
    'ingest_outsource_for_grade' :     'colour/media/for_flame/graded/opClips',

    # Export preset
    'first_export_preset':         'EXPORT_TO_GRADE/SHOT_PUBLISH_FOR_GRADE',
    'first_export_preset_mov':     'EXPORT_TO_GRADE/MOV_PUBLISH_FOR_GRADE',
    'layer_export_preset_mov':     'EXPORT_TO_GRADE/MOV_LAYER_PUBLISH_FOR_GRADE',
    'layer_export_preset':         'EXPORT_TO_GRADE/LAYER_PUBLISH_FOR_GRADE',
    'comp_export_preset' :         'EXPORT_TO_GRADE/COMP_PUBLISH_FOR_GRADE',
    'edl_export_preset' :          'EXPORT_TO_GRADE/EDL_PUBLISH',
    'edl_ref_export_preset' :      'EXPORT_TO_GRADE/EDL_REF_PUBLISH',

    # Naming conventions
    'first_export_suffix':  '_comp_render_main_v000',
    'graded_suffix':        '_comp_render_main',

    # Validation patterns
    'pattern_first_export': r'^[A-Za-z]{3}_\d{3}_L1$',
    'pattern_layer':        r'^[A-Za-z]{3}_\d{3}_L[2-9]\d*$',
    'pattern_comp_update':  r'^[A-Za-z]{3}_\d{3}_comp_render_main_v\d+$',
    'pattern_outsource'  :  r'^[a-z]{3}_shots_[A-Z]{3}_\d{3}$',
    'pattern_comp_no_version': r'^[A-Za-z]{3}_\d{3}_comp_render_main$'

}