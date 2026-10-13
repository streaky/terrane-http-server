{{ data.indent }}if slot{{ data.index }}.value.kind != 'list'
{{ data.indent }}    throw coercion-error
{{ data.indent }}decoded-items{{ data.index }} list of {{ data.item-class }} = list
{{ data.indent }}item-index{{ data.index }} int = 0
{{ data.indent }}while item-index{{ data.index }} < (slot{{ data.index }}.value.length;)
{{ data.indent }}    item{{ data.index }} = slot{{ data.index }}.value.item; item-index{{ data.index }}
{{ data.item-check }}{{ data.indent }}    decoded-items{{ data.index }}.append; {{ data.item-expression }}
{{ data.indent }}    item-index{{ data.index }} = item-index{{ data.index }} + 1
{{ data.indent }}value.{{ data.member }} = decoded-items{{ data.index }}
