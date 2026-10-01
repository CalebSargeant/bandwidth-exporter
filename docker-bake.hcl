# docker-bake.hcl: the build definition, consumed by `docker buildx bake`.
#
#   Local single-arch build:  docker buildx bake app-local
#   Multi-arch build (CI):    docker buildx bake app
#   Override the version:     VERSION=1.2.3 docker buildx bake app

variable "VERSION" { default = "dev" }
variable "REGISTRY" { default = "ghcr.io" }
variable "IMAGE_NAME" { default = "calebsargeant/bandwidth-exporter" }
variable "PLATFORMS" { default = "linux/amd64,linux/arm64" }
variable "REVISION" { default = "" }

group "default" {
  targets = ["app"]
}

target "app" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "runtime"
  platforms  = split(",", PLATFORMS)
  args = {
    REVISION = REVISION
  }
  tags = [
    "${REGISTRY}/${IMAGE_NAME}:${VERSION}",
  ]
}

# Single-arch so it can be loaded into the local Docker engine.
target "app-local" {
  inherits  = ["app"]
  platforms = ["linux/amd64"]
  tags      = ["bandwidth-exporter:dev"]
  output    = ["type=docker"]
}
