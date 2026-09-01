terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

# --- CONFIGURATION VARIABLES ---
variable "region" { default = "northamerica-northeast2" }
variable "zone" { default = "northamerica-northeast2-a" }
variable "machine_type" {
  # g2-standard-8 includes 1x NVIDIA L4 GPU with 24GB VRAM.
  # Use an A2 machine type only after requesting NVIDIA A100 quota.
  default = "g2-standard-12"
}

variable "hf_api_token" {
  description = "Hugging Face endpoint and local Qwen model Summarizer"
  sensitive   = true
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# --- PERSISTENT BOOT DISK ---
resource "google_compute_disk" "qwen_boot_disk" {
  name = "qwen-boot-disk"
  # Standard persistent disk or balanced is required for accelerator-optimized boot disks.
  type  = "pd-balanced"
  zone  = var.zone
  image = "ubuntu-os-cloud/ubuntu-2204-lts"
  size  = 250
}

# --- COMPUTE INSTANCE ---
resource "google_compute_instance" "qwen_worker" {
  name         = "qwen-l4-spot-tf"
  machine_type = var.machine_type
  zone         = var.zone

  # NOTE: For G2/A2 machine types the GPU is implicit.
  # GPUs cannot live-migrate, so on_host_maintenance must be TERMINATE.
  scheduling {
    provisioning_model          = "SPOT"
    preemptible                 = true
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    instance_termination_action = "STOP"
  }

  boot_disk {
    source      = google_compute_disk.qwen_boot_disk.name
    auto_delete = false
  }

  network_interface {
    network = "default"
    access_config {} # Assigns a public IP
  }

  service_account {
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -uxo pipefail

      # Idempotent: the startup-script runs on every boot. Skip once setup is done.
      SETUP_MARKER=/opt/qwen_setup_done
      if [ -f "$SETUP_MARKER" ]; then
        exit 0
      fi

      apt-get update
      apt-get install -y python3-pip git linux-headers-$(uname -r)

      # --- NVIDIA L4 driver (Ubuntu 22.04) ---
      curl -fsSL -O https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
      dpkg -i cuda-keyring_1.1-1_all.deb
      apt-get update
      apt-get install -y cuda-drivers

      # --- Python deps. torch ships its own CUDA 12.1 runtime; bitsandbytes enables --load-in-4bit. ---
      pip3 install --upgrade pip
      pip3 install torch --index-url https://download.pytorch.org/whl/cu121
      pip3 install "transformers>=4.42" accelerate bitsandbytes sentencepiece \
        huggingface_hub google-cloud-storage python-dotenv

      huggingface-cli login --token ${var.hf_api_token} || true

      touch "$SETUP_MARKER"
      # The driver kernel module needs a reboot to load on first install.
      reboot
    EOT
  }
}

output "ssh_command" {
  value = "gcloud compute ssh ${google_compute_instance.qwen_worker.name} --zone=${var.zone}"
}
