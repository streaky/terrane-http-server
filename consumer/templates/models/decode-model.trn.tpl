function {{ data.name }}-from-document {{ data.name }} throws throwable; document document-value
    if document.kind != 'map'
        throw coercion-error
    key-index int = 0
    while key-index < (document.length;)
        key = document.key; key-index
        if {{ data.unknown-field-condition }}
            throw coercion-error
        key-index = key-index + 1
    value = instance {{ data.name }};
