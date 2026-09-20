# winnow image.
#
# Two stages so the runtime carries no toolchain: the build stage installs uv
# and a managed CPython, the runtime stage gets the resulting virtualenv and
# nothing else.
#
# Deviation from the house UBI default, recorded rather than hidden: UBI 9 has
# no Python 3.14, which the inbound listener needs for imaplib's IDLE support.
# uv installs a standalone interpreter into the image instead, so the base stays
# UBI and the interpreter is pinned by the lockfile rather than by the distro.

FROM registry.access.redhat.com/ubi9/ubi:latest AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/winnow \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --no-editable: an editable install would point the runtime stage at /src,
# which only exists in the build stage.
RUN uv sync --frozen --no-dev --no-editable


FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

# The 1Password CLI resolves every credential at call time; nothing is baked in.
RUN microdnf install -y --nodocs shadow-utils ca-certificates \
    && microdnf clean all \
    && useradd --system --create-home --home-dir /var/lib/winnow --uid 1000 winnow

COPY --from=build /opt/python /opt/python
COPY --from=build /opt/winnow /opt/winnow
COPY --from=docker.io/1password/op:2 /usr/local/bin/op /usr/local/bin/op
# The seed lists are part of the product: the quickstart tells people to run
# `winnow company add --from developer-tools`, and that has to work in the
# artifact they actually run rather than only in a checkout.
COPY seeds /usr/share/winnow/seeds

ENV PATH="/opt/winnow/bin:${PATH}" \
    WINNOW_DB=/var/lib/winnow/winnow.db \
    WINNOW_PROFILE=/etc/winnow/profile.yaml

USER winnow
WORKDIR /var/lib/winnow

ENTRYPOINT ["winnow"]
CMD ["--help"]
