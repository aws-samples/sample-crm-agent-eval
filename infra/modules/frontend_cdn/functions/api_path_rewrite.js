// SPDX-License-Identifier: MIT
// CloudFront viewer-request function for the `/api/*` behaviour: rewrites
// `/api/<rest>` to `/${api_gateway_stage_name}/<rest>` so a request the SPA
// sends to `apiBaseUrl = "/api"` lands on the REST API's stage-qualified route.
// Rendered by Terraform (`templatefile`); `${api_gateway_stage_name}` is the
// deployed stage name, never a hardcoded literal.
function handler(event) {
  var request = event.request;
  var prefix = '/api';
  if (request.uri === prefix) {
    request.uri = '/${api_gateway_stage_name}';
  } else if (request.uri.startsWith(prefix + '/')) {
    request.uri = '/${api_gateway_stage_name}' + request.uri.substring(prefix.length);
  }
  return request;
}
