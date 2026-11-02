/**
 * Resource policy letting only this Gateway (by `source_arn`) invoke the REQUEST interceptor
 * Lambda.
 */

resource "aws_lambda_permission" "gateway_invoke_interceptor" {
  statement_id  = "AllowGatewayInvokeIdentityInterceptor"
  action        = "lambda:InvokeFunction"
  function_name = var.identity_interceptor_lambda_function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_bedrockagentcore_gateway.this.gateway_arn
}
