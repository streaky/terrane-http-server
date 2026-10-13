
function validation-response http-response throws throwable; message string
    entries = instance document-map-entries;
    entries.append; 'detail', (make-document-string; message)
    payload = make-document-map; entries
    if payload.failed
        throw coercion-error
    return create-json-response; 422, payload.value
