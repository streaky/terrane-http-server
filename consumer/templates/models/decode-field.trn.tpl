    slot{{ data.index }} = document.field; {{ data.wire }}
{% if data.required %}    if slot{{ data.index }}.failed
        throw coercion-error
{% endif %}    if not slot{{ data.index }}.failed
{% if data.optional %}        if slot{{ data.index }}.value.kind == 'none'
            value.{{ data.member }} = none
        else
{% endif %}{{ data.assignment }}