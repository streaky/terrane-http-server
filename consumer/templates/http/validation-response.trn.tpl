
function validation-response wire-response throws throwable; message string
    entries = instance document-map-entries;
    entries.append; 'detail', (make-document-string; message)
    payload = make-document-map; entries
    if payload.failed
        throw coercion-error
    response = encode-document-response; payload.value, 'json'
    response.status = 422
    return response
