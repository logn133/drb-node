import os
from internal.op25_liq_template import liquidsoap_config_template
from models import IcecastConfig

# P25 conventional with two channels (node-26#24): OP25 sends channel A's
# audio to UDP 23456 and channel B's to 23457, and audio.py -2 puts them on the
# left and right of the stereo stream. Every output below is mean(input) — mono
# — so a lone talker on one side would come out 6 dB down; doubling first keeps
# the common case (one channel keyed) at the same level as single-channel mode.
# Liquidsoap mixes in float, so the doubling cannot clip before the output.
_TWO_CHANNEL_AUDIO_ARGS = " -2"
_TWO_CHANNEL_MIX = "input = amplify(2.0, input)  # two conventional channels, L=A R=B"


def render_liquid_script(config: IcecastConfig, two_channel: bool = False) -> str:
    content = liquidsoap_config_template
    for key, value in config.model_dump().items():
        content = content.replace(f"${{{key}}}", str(value))
    content = content.replace("${audio_py_args}", _TWO_CHANNEL_AUDIO_ARGS if two_channel else "")
    content = content.replace("${two_channel_mix}", _TWO_CHANNEL_MIX if two_channel else "")
    return content


def generate_liquid_script(config: IcecastConfig, two_channel: bool = False):
    """
    Generates the "*.liq" file that's run by OP25 on startup. 

    Placeholders in the template must be formatted as ${VARIABLE_NAME}.

    Args:
        config (dict): A dictionary of key-value pairs for substitution.
                          Keys should match the variable names in the template (e.g., 'icecast_host').
    """
    try:
        content = render_liquid_script(config, two_channel)

        # Write the processed content to the output path
        output_path = "/configs/op25.liq"
        with open(output_path, 'w') as f:
            f.write(content)

        print(f"\nSuccessfully wrote processed configuration to: {output_path}")

    except FileNotFoundError:
        print(f"Error: Template file not found at {template_path}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")