output "fqdn" {
  description = "Повне доменне ім'я стенда (придатне для Let's Encrypt)"
  value       = azurerm_public_ip.main.fqdn
}

output "public_ip" {
  description = "Публічна адреса віртуальної машини"
  value       = azurerm_public_ip.main.ip_address
}

output "ssh" {
  description = "Команда підключення"
  value       = "ssh ${var.admin_username}@${azurerm_public_ip.main.fqdn}"
}

output "url" {
  description = "Адреса застосунку після налаштування Caddy"
  value       = "https://${azurerm_public_ip.main.fqdn}"
}

output "cost_note" {
  description = "Нагадування про економію кредиту"
  value       = <<-TXT
    Між демонстраціями зупиняйте обчислення:
      az vm deallocate -g ${azurerm_resource_group.main.name} -n ${azurerm_linux_virtual_machine.main.name}
    Повернення до роботи:
      az vm start -g ${azurerm_resource_group.main.name} -n ${azurerm_linux_virtual_machine.main.name}
    Якщо забракне пам'яті:
      az vm resize -g ${azurerm_resource_group.main.name} -n ${azurerm_linux_virtual_machine.main.name} --size Standard_B1ms
  TXT
}
