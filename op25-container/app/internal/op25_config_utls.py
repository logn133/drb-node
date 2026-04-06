import csv
import json
from models import TalkgroupTag
from typing import List
from internal.logger import create_logger

LOGGER = create_logger(__name__)

def save_talkgroup_tags(talkgroup_tags: List[TalkgroupTag]) -> None:
    """
    Writes a list of tags to the tags file.

    Args:
        talkgroup_tags (List[TalkgroupTag]): The list of TalkgroupTag instances.
    """
    with open("/configs/active.cfg.tags.tsv", 'w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file, delimiter='\t', lineterminator='\n')
        # Write rows
        for tag in talkgroup_tags:
            writer.writerow([tag.tagDec, tag.talkgroup])

def save_whitelist(talkgroup_tags: List[int]) -> None:
    """
    Writes a list of talkgroups to the whitelists file.

    Args:
        talkgroup_tags (List[int]): The list of decimals to whitelist.
    """
    with open("/configs/active.cfg.whitelist.tsv", 'w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file, delimiter='\t', lineterminator='\n')
        # Write rows
        for tag in (talkgroup_tags or []):
            writer.writerow([tag])

def del_none_in_dict(d):
    """
    Delete keys with the value ``None`` in a dictionary, recursively.

    This alters the input so you may wish to ``copy`` the dict first.
    """
    for key, value in list(d.items()):
        LOGGER.info(f"Key: '{key}'\nValue: '{value}'")
        if value is None:
            del d[key]
        elif isinstance(value, dict):
            del_none_in_dict(value)
        elif isinstance(value, list):
            for iterative_value in value:
                del_none_in_dict(iterative_value)
    return d  # For convenience

def get_current_system_from_config() -> str:
    # Get the current config
    with open('/configs/active.cfg.json', 'r') as f:
        json_data = f.read()
        if isinstance(json_data, str):
            try:
                data = json.loads(json_data)
            except json.JSONDecodeError:
                return None
        elif isinstance(json_data, dict):
            data = json_data
        else:
            return None

        if "channels" in data and isinstance(data["channels"], list) and len(data["channels"]) > 0:
            first_channel = data["channels"][0]
            if "name" in first_channel:
                return first_channel["name"]
    return None
