function {{ data.name }}-to-document document-value; value {{ data.name }}
    entries = instance document-map-entries;
{{ data.fields }}    encoded-document = make-document-map; entries
    if encoded-document.failed
        throw coercion-error
    return encoded-document.value

