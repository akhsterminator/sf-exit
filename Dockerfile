FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[all]"

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/akhsterminator/sf-exit" \
      org.opencontainers.image.description="Move all of an organisation's data out of Salesforce" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels "sf-exit[all]" \
 && rm -rf /wheels \
 && useradd --create-home sfexit
USER sfexit
WORKDIR /work
ENTRYPOINT ["sf-exit"]
CMD ["--help"]
