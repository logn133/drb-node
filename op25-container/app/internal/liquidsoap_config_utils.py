import os
from internal.op25_liq_template import liquidsoap_config_template
from models import IcecastConfig

def generate_liquid_script(config: IcecastConfig):
    """
    Generates the "*.liq" file that's run by OP25 on startup. 

    Placeholders in the template must be formatted as ${VARIABLE_NAME}.

    Args:
        config (dict): A dictionary of key-value pairs for substitution.
                          Keys should match the variable names in the template (e.g., 'icecast_host').
    """
    try:
        content = liquidsoap_config_template
        # Replace variables
        for key, value in config.model_dump().items():
            placeholder = f"${{{key}}}"
            # Ensure the value is converted to string for replacement
            content = content.replace(placeholder, str(value))
            print(f"  - Replaced placeholder {placeholder}")

        # Write the processed content to the output path
        output_path = "/configs/op25.liq"
        with open(output_path, 'w') as f:
            f.write(content)

        print(f"\nSuccessfully wrote processed configuration to: {output_path}")

    except FileNotFoundError:
        print(f"Error: Template file not found at {template_path}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")