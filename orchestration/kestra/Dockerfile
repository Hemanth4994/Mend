# Kestra without the ~4.7 GB of plugins the stock image bundles, plus the one plugin the Mend flows use (the shell `Commands` task).
# Everything else the flows use (Log, If, Pause, Subflow, Fail, the Schedule trigger, the Docker task runner) is part of Kestra itself.
# The -slim tag is Kestra's name for the image without plugins (formerly -no-plugins). Keep the version in step with the flows:
# they were checked against Kestra v2.0.10's source.
FROM kestra/kestra:v2.0.10-slim

# Downloads from Maven Central at build time. Open-source Kestra installs the latest plugin version compatible with this Kestra version.
RUN /app/kestra plugins install io.kestra.plugin:plugin-script-shell:LATEST
