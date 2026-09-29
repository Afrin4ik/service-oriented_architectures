workspace "Маркетплейс" "Целевая архитектура маркетплейса. В задании 1 реализован только каркас Catalog Service." {
    model {
        buyer = person "Покупатель" "Просматривает ленту, оформляет и оплачивает заказ"
        seller = person "Продавец" "Управляет товарами и исполнением заказов"

        psp = softwareSystem "Платёжный провайдер" "Принимает оплату, сообщает результат" "External"
        mail = softwareSystem "Email-провайдер" "Доставляет уведомления" "External"

        marketplace = softwareSystem "Маркетплейс" "Каталог товаров, персонализированная лента, заказы, оплата и уведомления" {
            web = container "Web Application" "Витрина и личные кабинеты" "TypeScript / React"
            gateway = container "API Gateway" "Единая точка входа, TLS, маршрутизация" "Nginx"

            users = container "User Service" "Учётные записи, роли, контакты, интересы" "Python / FastAPI"
            catalog = container "Catalog Service" "Товары, категории и актуальные цены" "Python / FastAPI"
            feed = container "Feed Service" "Персонализированная выдача товаров" "Python / FastAPI"
            orders = container "Order Service" "Состав, сумма и жизненный цикл заказа" "Python / FastAPI"
            payments = container "Payment Service" "Платёжные попытки и учёт оплаты" "Python / FastAPI"
            notifications = container "Notification Service" "Шаблоны, отправка и повторные попытки" "Python / worker"

            broker = container "Event Broker" "Доставка событий подписчикам" "RabbitMQ / AMQP" "Queue"

            users_db = container "User DB" "Аккаунты, роли, контакты, интересы" "PostgreSQL" "Database"
            catalog_db = container "Catalog DB" "Товары, категории, цены, публикация" "PostgreSQL" "Database"
            feed_db = container "Feed DB" "Проекция товаров, интересы, популярность" "PostgreSQL" "Database"
            orders_db = container "Order DB" "Заказы, снимки позиций, статусы" "PostgreSQL" "Database"
            payments_db = container "Payment DB" "Платежи, суммы, ID провайдера" "PostgreSQL" "Database"
            notifications_db = container "Notification DB" "Шаблоны и журнал доставки" "PostgreSQL" "Database"
        }

        buyer -> web "Пользуется витриной" "HTTPS"
        seller -> web "Пользуется кабинетом" "HTTPS"
        web -> gateway "Вызывает API" "HTTPS / JSON, синхр."
        gateway -> users "Аккаунты и интересы" "HTTP / JSON, синхр."
        gateway -> catalog "Карточки и управление товарами" "HTTP / JSON, синхр."
        gateway -> feed "Получает ленту" "HTTP / JSON, синхр."
        gateway -> orders "Создаёт и читает заказы, меняет статус" "HTTP / JSON, синхр."
        gateway -> payments "Передаёт webhook провайдера" "HTTP / JSON, синхр."
        orders -> catalog "Проверяет товары, получает цены" "HTTP / JSON, синхр."
        orders -> payments "Создаёт платёж, получает ссылку" "HTTP / JSON, синхр."
        notifications -> users "Получает контакт адресата" "HTTP / JSON, синхр."
        payments -> psp "Создаёт платёж, сверяет результат" "HTTPS / JSON, синхр."
        buyer -> psp "Оплачивает по полученной ссылке" "HTTPS"
        psp -> gateway "Сообщает результат оплаты" "HTTPS webhook, асинхр." "Asynchronous"
        notifications -> mail "Передаёт письмо для доставки" "HTTPS API, синхр."

        users -> broker "UserPreferencesChanged" "AMQP, асинхр." "Asynchronous"
        catalog -> broker "ProductChanged" "AMQP, асинхр." "Asynchronous"
        orders -> broker "OrderStatusChanged" "AMQP, асинхр." "Asynchronous"
        payments -> broker "PaymentSucceeded / PaymentFailed" "AMQP, асинхр." "Asynchronous"
        broker -> feed "Товары, интересы, оплаченные заказы" "AMQP, асинхр." "Asynchronous"
        broker -> orders "Результат оплаты" "AMQP, асинхр." "Asynchronous"
        broker -> notifications "Статусы заказов" "AMQP, асинхр." "Asynchronous"

        users -> users_db "Читает и записывает" "SQL / TCP"
        catalog -> catalog_db "Читает и записывает" "SQL / TCP"
        feed -> feed_db "Читает и записывает" "SQL / TCP"
        orders -> orders_db "Читает и записывает" "SQL / TCP"
        payments -> payments_db "Читает и записывает" "SQL / TCP"
        notifications -> notifications_db "Читает и записывает" "SQL / TCP"
    }

    views {
        systemContext marketplace "Context" {
            title "Маркетплейс — C4 System Context"
            include *
            autoLayout lr
        }

        container marketplace "Containers" {
            title "Маркетплейс — C4 Container (целевая архитектура)"
            include *
            autoLayout tb
            default
        }

        styles {
            element "Element" {
                color #ffffff
            }
            element "Person" {
                shape Person
                background #08427b
            }
            element "Software System" {
                background #1168bd
            }
            element "Container" {
                background #438dd5
            }
            element "External" {
                background #777777
            }
            element "Database" {
                shape Cylinder
                background #d6e8f8
                color #123b60
            }
            element "Queue" {
                shape Pipe
            }
            relationship "Relationship" {
                color #596a7b
                style solid
            }
            relationship "Asynchronous" {
                style dashed
            }
        }
    }
}
