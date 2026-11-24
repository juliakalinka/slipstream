# Параметри розгортання. Значення за замовчуванням розраховані на
# демонстраційний стенд, а не на промислову експлуатацію.

variable "subscription_id" {
  description = "Ідентифікатор студентської підписки Azure"
  type        = string
}

variable "workload" {
  description = "Назва навантаження для схеми іменування Microsoft CAF"
  type        = string
  default     = "slipstream"
}

variable "environment" {
  description = "Середовище: demo, dev, prod"
  type        = string
  default     = "demo"
}

variable "location" {
  description = "Регіон Azure. Poland Central - найближчий до Львова"
  type        = string
  default     = "polandcentral"
}

variable "location_abbr" {
  description = "Скорочення регіону для імен ресурсів"
  type        = string
  default     = "plc"
}

variable "vm_size" {
  description = <<-TXT
    Розмір віртуальної машини. B1s - 1 vCPU, 1 ГіБ, близько 8,76 USD/міс.
    Якщо під час демонстрації забракне пам'яті на перенавчання моделей,
    перехід на B1ms виконується командою az vm resize без перестворення.
  TXT
  type        = string
  default     = "Standard_B1s"
}

variable "os_disk_size_gb" {
  description = "Розмір диска ОС, ГіБ (StandardSSD E4 = 32 ГіБ)"
  type        = number
  default     = 32
}

variable "admin_username" {
  description = "Обліковий запис адміністратора на віртуальній машині"
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key_path" {
  description = "Відкритий ключ SSH для доступу до машини"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "allowed_ssh_cidr" {
  description = <<-TXT
    Звідки дозволено SSH. За замовчуванням - лише поточна адреса автора.
    Якщо провайдер змінить адресу, оновіть значення і виконайте apply.
  TXT
  type        = string
}

variable "dns_label" {
  description = <<-TXT
    Мітка DNS для публічної адреси. Має бути унікальною в межах регіону;
    повне ім'я матиме вигляд <мітка>.polandcentral.cloudapp.azure.com
    і його достатньо для сертифіката Let's Encrypt.
  TXT
  type        = string
  default     = "slipstream-demo-plc"
}
