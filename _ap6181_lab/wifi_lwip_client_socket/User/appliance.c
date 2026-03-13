#include "wifi_base_config.h"
#include "appliance.h"
#include "stm32H7xx.h"
/* FreeRTOS头文件 */
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "client.h"

#define PRINTF printf

/**
 * @brief app_main
 *
 */
void app_main( void )
{
		PRINTF("[boot] app_main entered\r\n");
		PRINTF("[boot] waiting 1500ms for AP6181 cold start\r\n");
		vTaskDelay(pdMS_TO_TICKS(1500));
	
		/*配置wifi lwip信息*/
		Config_WIFI_LwIP_Info();
	
		client_init();
}

