/**
 * ECR repository for the AgentCore Runtime image, plus the CodeBuild project that builds and
 * pushes it during apply. The build reruns only when `var.source_content_hash` changes.
 *
 * A failed build fails `terraform apply` at `null_resource.image_build`; resources applied earlier
 * in the run stay in place, and re-running `apply` after a fix retries the build.
 */

# ---------------------------------------------------------------------------
# ECR repository: image scanning on push, lifecycle policy, CMK encryption.
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "agent" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"

  # Holds build artefacts only; a destroy of a repository still holding images otherwise fails with
  # RepositoryNotEmptyException.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the most recent ${var.image_retention_count} tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = var.image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# CodeBuild project: builds the container image from the agent source archive
# and pushes it to the ECR repository above.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "codebuild_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.name_prefix}-codebuild-role"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "codebuild_permissions" {
  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.codebuild.arn}:*"]
  }

  statement {
    sid    = "EcrAuth"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
    ]
    resources = [aws_ecr_repository.agent.arn]
  }

  # Scoped to the one source object, not `bucket/*`: the bucket also holds published sweep output.
  statement {
    sid       = "SourceObject"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${var.source_bucket_arn}/${var.source_key}"]
  }

  # The eval-artefacts CMK encrypts both the source object and the project (`encryption_key`). The
  # key's own policy must also admit this role.
  statement {
    sid       = "ProjectKms"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = [var.source_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "${var.name_prefix}-codebuild"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild_permissions.json
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.name_prefix}-agent-image"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_codebuild_project" "agent_image" {
  # checkov:skip=CKV_AWS_316: privileged mode is required to run docker build for the Runtime image
  name           = "${var.name_prefix}-agent-image"
  description    = "Builds and pushes the AgentCore Runtime container image."
  build_timeout  = 30
  service_role   = aws_iam_role.codebuild.arn
  encryption_key = var.source_kms_key_arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"
    type                        = "ARM_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true

    environment_variable {
      name  = "ECR_REPOSITORY_URI"
      value = aws_ecr_repository.agent.repository_url
    }

    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = var.aws_region
    }
  }

  source {
    type      = "S3"
    location  = var.source_bucket_arn == "" ? "" : "${replace(var.source_bucket_arn, "arn:aws:s3:::", "")}/${var.source_key}"
    buildspec = var.buildspec
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# The in-apply build trigger, rerun when `triggers.source_content_hash` changes.
# ---------------------------------------------------------------------------

resource "null_resource" "image_build" {
  triggers = {
    source_content_hash = var.source_content_hash
    project_name        = aws_codebuild_project.agent_image.name
  }

  # Starts the build and polls `batch-get-builds` until a terminal status (CodeBuild has no CLI
  # waiter), bounded by the project's 30-minute `build_timeout` plus margin; a failed build fails
  # the apply.
  #
  # A failed `start-build` is retried with backoff, and a build that fails in its first phases is
  # restarted once; any other terminal build status fails the apply.
  #
  # The repository is IMMUTABLE, so if `aws ecr describe-images` already finds `var.image_tag`, the
  # build is skipped. `ImageNotFoundException` proceeds to a build; any other `describe-images` error
  # fails the apply.
  provisioner "local-exec" {
    command = <<-EOT
      set -euo pipefail

      IMAGE_TAG="${var.image_tag}"

      DESCRIBE_OUTPUT=$(aws ecr describe-images \
        --repository-name ${aws_ecr_repository.agent.name} \
        --image-ids imageTag="$IMAGE_TAG" \
        --profile ${var.aws_profile} \
        --region ${var.aws_region} \
        --no-cli-pager \
        --query 'imageDetails[0].imageDigest' --output text 2>/tmp/ecr_describe_err.$$) && DESCRIBE_STATUS=0 || DESCRIBE_STATUS=$?

      if [ "$DESCRIBE_STATUS" -eq 0 ]; then
        echo "Image tag $IMAGE_TAG already exists in ${aws_ecr_repository.agent.name} (digest $DESCRIBE_OUTPUT); this exact content is already pushed under an IMMUTABLE repository, skipping rebuild." >&2
        rm -f /tmp/ecr_describe_err.$$
        exit 0
      fi

      if ! grep -q "ImageNotFoundException" /tmp/ecr_describe_err.$$ 2>/dev/null; then
        echo "describe-images failed for tag $IMAGE_TAG for a reason other than the tag being absent; not proceeding to build:" >&2
        cat /tmp/ecr_describe_err.$$ >&2
        rm -f /tmp/ecr_describe_err.$$
        exit 1
      fi
      rm -f /tmp/ecr_describe_err.$$

      MAX_START_ATTEMPTS=6
      START_BACKOFF_SECONDS=10
      POLL_INTERVAL_SECONDS=15
      MAX_POLL_SECONDS=1800   # matches build_timeout = 30 minutes, plus poll granularity

      BUILD_ID=""
      attempt=1
      while [ "$attempt" -le "$MAX_START_ATTEMPTS" ]; do
        if BUILD_ID=$(aws codebuild start-build \
          --project-name ${aws_codebuild_project.agent_image.name} \
          --profile ${var.aws_profile} \
          --region ${var.aws_region} \
          --no-cli-pager \
          --query 'build.id' --output text 2>/tmp/codebuild_start_build_err.$$); then
          break
        fi
        echo "start-build attempt $attempt/$MAX_START_ATTEMPTS failed:" >&2
        cat /tmp/codebuild_start_build_err.$$ >&2
        rm -f /tmp/codebuild_start_build_err.$$
        if [ "$attempt" -eq "$MAX_START_ATTEMPTS" ]; then
          echo "start-build did not succeed after $MAX_START_ATTEMPTS attempts" >&2
          exit 1
        fi
        sleep "$START_BACKOFF_SECONDS"
        attempt=$((attempt + 1))
      done
      rm -f /tmp/codebuild_start_build_err.$$

      elapsed=0
      restarted=0
      STATUS="IN_PROGRESS"
      PHASE=""
      while [ "$elapsed" -lt "$MAX_POLL_SECONDS" ]; do
        # A terminal build reports currentPhase COMPLETED; the failed phase is the first entry in
        # phases[] whose phaseStatus is not SUCCEEDED.
        POLL=$(aws codebuild batch-get-builds \
          --profile ${var.aws_profile} \
          --region ${var.aws_region} \
          --ids "$BUILD_ID" \
          --no-cli-pager \
          --query "builds[0].[buildStatus, phases[?phaseStatus && phaseStatus != 'SUCCEEDED'] | [0].phaseType]" \
          --output text)
        read -r STATUS PHASE <<<"$POLL"

        case "$STATUS" in
          SUCCEEDED)
            echo "CodeBuild run $BUILD_ID succeeded."
            exit 0
            ;;
          IN_PROGRESS)
            ;;
          FAILED|FAULT|TIMED_OUT|STOPPED)
            # A FAILED build whose failed phase is SUBMITTED, QUEUED or PROVISIONING (before the
            # source or docker build ran) gets one fresh start-build; a second early failure, or
            # any other terminal failure, fails the apply.
            if [ "$STATUS" = "FAILED" ] && \
               { [ "$PHASE" = "SUBMITTED" ] || [ "$PHASE" = "QUEUED" ] || [ "$PHASE" = "PROVISIONING" ]; }; then
              if [ "$restarted" -eq 1 ]; then
                echo "Build $BUILD_ID failed early in phase $PHASE after the build was already restarted once; failing the apply." >&2
                exit 1
              fi
              restarted=1
              echo "Build $BUILD_ID failed early in phase $PHASE (likely IAM propagation race); retrying once with a fresh start-build after a settling delay." >&2
              sleep "$START_BACKOFF_SECONDS"
              BUILD_ID=$(aws codebuild start-build \
                --project-name ${aws_codebuild_project.agent_image.name} \
                --profile ${var.aws_profile} \
                --region ${var.aws_region} \
                --no-cli-pager \
                --query 'build.id' --output text)
              elapsed=0
              continue
            fi
            echo "CodeBuild run $BUILD_ID ended with status $STATUS in phase $PHASE" >&2
            exit 1
            ;;
          *)
            echo "CodeBuild run $BUILD_ID reported unrecognized status $STATUS" >&2
            exit 1
            ;;
        esac

        sleep "$POLL_INTERVAL_SECONDS"
        elapsed=$((elapsed + POLL_INTERVAL_SECONDS))
      done

      echo "CodeBuild run $BUILD_ID did not reach a terminal status within $MAX_POLL_SECONDS seconds (last status: $STATUS)" >&2
      exit 1
    EOT
  }

  depends_on = [aws_codebuild_project.agent_image]
}
