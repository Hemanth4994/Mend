{% test timestamp_between(model, column_name, min_value, max_value) %}
select * from {{ model }}
where {{ column_name }} is not null
  and ({{ column_name }} < cast('{{ min_value }}' as timestamptz) or {{ column_name }} > cast('{{ max_value }}' as timestamptz))
{% endtest %}
