import json
import toml


def json_to_toml(json_bytes: bytes) -> bytes:
    """
    Convert JSON to TOML.

    Args:
        json_bytes: JSON content as bytes

    Returns:
        TOML content as bytes

    Raises:
        ValueError: If JSON is invalid or conversion fails
    """
    try:
        json_str = json_bytes.decode('utf-8')
        data = json.loads(json_str)

        if isinstance(data, list):
            data = {"items": data}
        elif not isinstance(data, dict):
            data = {"value": data}

        toml_str = toml.dumps(data)

        return toml_str.encode('utf-8')
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {str(e)}")
    except Exception as e:
        raise ValueError(f"JSON to TOML conversion failed: {str(e)}")


def toml_to_json(toml_bytes: bytes) -> bytes:
    """
    Convert TOML to JSON.

    Args:
        toml_bytes: TOML content as bytes

    Returns:
        JSON content as bytes

    Raises:
        ValueError: If TOML is invalid or conversion fails
    """
    try:
        toml_str = toml_bytes.decode('utf-8')
        data = toml.loads(toml_str)

        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        return json_str.encode('utf-8')
    except toml.TomlDecodeError as e:
        raise ValueError(f"Invalid TOML: {str(e)}")
    except Exception as e:
        raise ValueError(f"TOML to JSON conversion failed: {str(e)}")
