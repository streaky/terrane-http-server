{{ data.indent }}encoded-items{{ data.index }} list of document-value = list
{{ data.indent }}item-index{{ data.index }} int = 0
{{ data.indent }}while item-index{{ data.index }} < {{ data.expression }}.length
{{ data.indent }}    encoded-items{{ data.index }}.append; {{ data.item-expression }}
{{ data.indent }}    item-index{{ data.index }} = item-index{{ data.index }} + 1
{{ data.indent }}encoded-list{{ data.index }} = make-document-list; encoded-items{{ data.index }}
{{ data.indent }}if encoded-list{{ data.index }}.failed
{{ data.indent }}    throw coercion-error
{{ data.indent }}entries.append; {{ data.wire }}, encoded-list{{ data.index }}.value
