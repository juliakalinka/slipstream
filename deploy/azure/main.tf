# Інфраструктура демонстраційного стенда Slipstream в Azure.
#
# Склад навмисно мінімальний: одна віртуальна машина, на якій засобами
# docker compose працюють PostgreSQL, застосунок і зворотний проксі Caddy.
# Керована база даних, балансувальник і реєстр образів не використовуються:
# для демонстрації вони не потрібні, а кредит студентської підписки обмежений.
#
# Імена ресурсів відповідають схемі Microsoft Cloud Adoption Framework:
#   <тип>-<навантаження>-<середовище>-<регіон>

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
  # Стан зберігається локально. Віддалене сховище стану для проєкту
  # з одним виконавцем було б надлишковим: воно потребує окремого
  # облікового запису зберігання, контейнера та механізму блокування.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  suffix = "${var.workload}-${var.environment}-${var.location_abbr}"

  tags = {
    workload    = var.workload
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "masters-thesis-demo"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.suffix}"
  location = var.location
  tags     = local.tags
}

# ─────────────────────────────────────────────────────────────── мережа
resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.suffix}"
  address_space       = ["10.20.0.0/16"]
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_subnet" "app" {
  name                 = "snet-app-${var.environment}-${var.location_abbr}"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.20.1.0/24"]
}

# Відкрито лише те, без чого демонстрація неможлива: доступ для
# адміністрування з однієї адреси та два порти веб-трафіку.
resource "azurerm_network_security_group" "main" {
  name                = "nsg-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags

  security_rule {
    name                       = "allow-ssh-from-admin"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.allowed_ssh_cidr
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-http"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-https"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_public_ip" "main" {
  name                = "pip-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = var.dns_label
  tags                = local.tags
}

resource "azurerm_network_interface" "main" {
  name                = "nic-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = azurerm_subnet.app.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.main.id
  }
}

resource "azurerm_network_interface_security_group_association" "main" {
  network_interface_id      = azurerm_network_interface.main.id
  network_security_group_id = azurerm_network_security_group.main.id
}

# ──────────────────────────────────────────────────── віртуальна машина
resource "azurerm_linux_virtual_machine" "main" {
  name                  = "vm-${local.suffix}"
  computer_name         = "vm-${local.suffix}"
  location              = azurerm_resource_group.main.location
  resource_group_name   = azurerm_resource_group.main.name
  size                  = var.vm_size
  admin_username        = var.admin_username
  network_interface_ids = [azurerm_network_interface.main.id]
  tags                  = local.tags

  # Автентифікація лише за ключем: паролів на машині немає.
  disable_password_authentication = true

  admin_ssh_key {
    username   = var.admin_username
    public_key = file(pathexpand(var.ssh_public_key_path))
  }

  os_disk {
    name                 = "osdisk-${local.suffix}"
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = var.os_disk_size_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  custom_data = base64encode(file("${path.module}/cloud-init.yaml"))
}
