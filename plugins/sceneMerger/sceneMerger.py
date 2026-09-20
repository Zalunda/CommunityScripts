import sys
import os
import json
from collections import defaultdict
import stashapi.log as log
from stashapi.stashapp import StashInterface

def main():
    input_data = {}
    if not sys.stdin.isatty():
        try:
            input_str = sys.stdin.read()
            if input_str.strip():
                input_data = json.loads(input_str)
        except Exception as e:
            log.error(f"Could not parse Stash input: {e}")

    # Determine what triggered the script
    plugin_args = input_data.get("args", {})
    mode = plugin_args.get("mode", "manual_dry_run")
    
    # If triggered by a hook, Stash passes the ID of the scene that was just updated
    hook_context = plugin_args.get("hookContext", {})
    hook_scene_id = hook_context.get("id")

    stash = StashInterface()
    config = stash.get_configuration().get("plugins", {})
    
    my_config = {}
    for key, val in config.items():
        if "automerge" in key.lower():
            my_config = val
            break
            
    allow_auto = my_config.get("AllowAutoMerge", False)
    target_stash_id = None

    # Determine execution behavior based on mode
    if mode == "hook":
        if not allow_auto:
            print(json.dumps({"output": "ok"}))
            return
            
        # Optimization: Fetch ONLY the scene that triggered this hook
        if hook_scene_id:
            scene_query = """query($id: ID!) { findScene(id: $id) { stash_ids { stash_id endpoint } } }"""
            scene_res = stash.callGQL(scene_query, {"id": hook_scene_id}).get("findScene")
            if not scene_res:
                print(json.dumps({"output": "ok"}))
                return
                
            # Find the StashDB ID for this specific scene
            for s in scene_res.get("stash_ids", []):
                if s.get("endpoint") == "https://stashdb.org/graphql":
                    target_stash_id = s.get("stash_id")
                    break
            
            # If the scene didn't get a StashDB ID, exit instantly.
            if not target_stash_id:
                print(json.dumps({"output": "ok"}))
                return

        is_dry_run = False
        
    elif mode == "manual_execute":
        is_dry_run = False
        log.info("Starting AutoMerge. Merging will be EXECUTED.")
        
    else: 
        is_dry_run = True
        log.info("AutoMerge is in DRY RUN mode. No database changes will be made.")


    # 1. Fetch Scenes (TARGETED VS GLOBAL)
    if mode == "hook" and target_stash_id:
        query = """
        query FindScenes($stash_id: String!) {
          findScenes(filter: { per_page: -1 }, scene_filter: { stash_id_endpoint: { stash_id: $stash_id, modifier: EQUALS } }) {
            scenes {
              id
              title
              files { path }
              stash_ids { stash_id endpoint }
            }
          }
        }
        """
        result = stash.callGQL(query, {"stash_id": target_stash_id})
        scenes = result['findScenes']['scenes']
    else:
        query = """
        query FindScenes {
          findScenes(filter: { per_page: -1 }) {
            scenes {
              id
              title
              files { path }
              stash_ids { stash_id endpoint }
            }
          }
        }
        """
        result = stash.callGQL(query)
        scenes = result['findScenes']['scenes']


    # 2. Group scenes by StashDB ID
    stash_id_groups = defaultdict(list)
    for scene in scenes:
        if not scene.get('stash_ids'):
            continue
            
        for sid in scene['stash_ids']:
            if sid['endpoint'] == "https://stashdb.org/graphql":
                stash_id_groups[sid['stash_id']].append(scene)

    # 3. Process groups to find duplicates in the same folder
    merge_operations = 0
    total_files_merged = 0
    
    for stash_id, duplicate_scenes in stash_id_groups.items():
        if len(duplicate_scenes) < 2:
            continue

        folder_groups = defaultdict(list)
        for scene in duplicate_scenes:
            if not scene.get('files'):
                continue
            
            file_path = scene['files'][0]['path']
            folder_path = os.path.dirname(file_path)
            folder_groups[folder_path].append(scene)

        # 4. Evaluate scenes that share the same folder
        for folder_path, scenes_in_folder in folder_groups.items():
            if len(scenes_in_folder) >= 2:
                
                # Sort scenes alphabetically by filename
                scenes_in_folder.sort(key=lambda s: os.path.basename(s['files'][0]['path']).lower())
                
                destination_scene = scenes_in_folder[0]
                source_scenes = scenes_in_folder[1:]

                destination_id = destination_scene['id']
                dest_title = destination_scene.get('title') or "Unknown Title"
                dest_filename = os.path.basename(destination_scene['files'][0]['path'])
                
                source_ids = [s['id'] for s in source_scenes]
                
                if is_dry_run:
                    log.info(f"[DRY RUN] StashID: {stash_id} | Folder: {folder_path}")
                    log.info(f"[DRY RUN] KEEPING: Scene {destination_id} '{dest_title}' ({dest_filename})")
                    
                    for source_scene in source_scenes:
                        src_id = source_scene['id']
                        src_filename = os.path.basename(source_scene['files'][0]['path'])
                        log.info(f"[DRY RUN] WOULD MERGE: '{src_filename}' (Scene {src_id}) -> '{dest_title}' (Scene {destination_id})")
                        
                    merge_operations += 1
                    total_files_merged += len(source_ids)
                else:
                    # Log a specific line for every single file being merged
                    for source_scene in source_scenes:
                        src_id = source_scene['id']
                        src_filename = os.path.basename(source_scene['files'][0]['path'])
                        log.info(f"Merged '{src_filename}' (Scene {src_id}) to '{dest_title}' (Scene {destination_id}) for StashID {stash_id}")

                    merge_mutation = """
                    mutation SceneMerge($input: SceneMergeInput!) {
                      sceneMerge(input: $input) {
                        id
                      }
                    }
                    """
                    variables = {
                        "input": {
                            "destination": destination_id,
                            "source": source_ids
                        }
                    }
                    
                    stash.callGQL(merge_mutation, variables)
                    merge_operations += 1
                    total_files_merged += len(source_ids)

    if is_dry_run:
        log.info(f"Dry run complete. Found {total_files_merged} scenes across {merge_operations} groups that would be merged.")
    else:
        # Only log completion summary if it actually did something or was a manual run
        if mode != "hook" or merge_operations > 0:
            log.info(f"AutoMerge complete. Performed {merge_operations} merge operations (Total {total_files_merged} scenes merged).")
    
    print(json.dumps({"output": "ok"}))

if __name__ == "__main__":
    main()