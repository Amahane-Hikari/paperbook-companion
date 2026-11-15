"""Explicit object-coordinate contracts shared by legacy and book readers."""
import math


def object_contract(raw, width, height, source_id=''):
    """Convert only an explicitly declared coordinate space; never guess by range."""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0 or not isinstance(raw, dict):
        raise ValueError('Object result requires an image size and JSON object')
    if 'error' in raw:
        raise ValueError('Vision analysis returned an error')
    space = raw.get('coordinate_space', 'pixel')
    if space not in {'pixel', 'normalized_1', 'normalized_1000'}:
        raise ValueError('Unsupported coordinate_space')
    items = raw.get('detections')
    if items is None:
        objects = raw.get('objects', raw)
        if not isinstance(objects, dict) or not objects:
            raise ValueError('Object detections must be a list or label-to-box mapping')
        if not all(isinstance(label, str) and isinstance(box, (list, tuple)) and len(box) == 4
                   for label, box in objects.items()):
            raise ValueError('Unknown or mixed object-result schema')
        items = [{'label': label, 'box': box} for label, box in objects.items()]
    if not isinstance(items, list):
        raise ValueError('detections must be a list')
    if space == 'pixel':
        ref_width = float(raw.get('reference_width', width))
        ref_height = float(raw.get('reference_height', height))
    else:
        ref_width = ref_height = 1.0 if space == 'normalized_1' else 1000.0
    if not math.isfinite(ref_width) or not math.isfinite(ref_height) or ref_width <= 0 or ref_height <= 0:
        raise ValueError('Invalid coordinate reference dimensions')
    detections = []
    for item in items[:100]:
        if not isinstance(item, dict):
            raise ValueError('Invalid detection item')
        box = item.get('box')
        label = item.get('label')
        if not isinstance(label, str) or not label.strip() or not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError('Detection requires a label and four coordinates')
        try:
            coordinates = [float(value) for value in box]
        except (ValueError, TypeError) as exc:
            raise ValueError('Detection coordinates must be numeric') from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError('Detection coordinates must be finite')
        x1, y1, x2, y2 = coordinates
        if x2 <= x1 or y2 <= y1:
            raise ValueError('Detection box must have positive area')
        pixel = [max(0, min(width, round(x1 * width / ref_width))),
                 max(0, min(height, round(y1 * height / ref_height))),
                 max(0, min(width, round(x2 * width / ref_width))),
                 max(0, min(height, round(y2 * height / ref_height)))]
        if pixel[2] <= pixel[0] or pixel[3] <= pixel[1]:
            continue
        detections.append({'label': label.strip(), 'box': pixel})
    return {'coordinate_space': 'pixel', 'reference_width': width,
            'reference_height': height, 'source_id': str(source_id), 'detections': detections}


def label_mapping(contract):
    """Compatibility representation for the original reader's object loop."""
    result = {}
    for detection in contract['detections']:
        label = detection['label']
        key = label if label not in result else f"{label} #{len(result) + 1}"
        result[key] = detection['box']
    return result
