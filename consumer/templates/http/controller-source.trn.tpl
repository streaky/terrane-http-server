
public class {{ data.name }} implements controller
{% if data.owner %}    private source {{ data.source-type }}

    function construct; source {{ data.source-type }}
        this.source = move source

{% endif %}    consuming function routes routes; registered-routes routes
{% for operation in data.operations %}{% if operation.selected %}        registered-routes = registered-routes.endpoint; (instance http-method::{{ operation.method-key }};), {{ operation.path-source }}, this.adapt-handler{{ operation.index }}{% if operation.negotiated %}, true{% endif %}
{% endif %}{% endfor %}{% if data.documented %}        registered-routes = registered-routes.endpoint; (instance http-method::get;), '/openapi.json', this.openapi-handler
{% endif %}        return registered-routes
{% if data.documented %}
    async function openapi-handler wire-response; _request incoming-request
        return encoded-response; 200, 'application/json', (openapi-json;)
{% endif %}{% for operation in data.operations %}{% if operation.selected %}
{{ operation.adapter }}{% endif %}{% endfor %}