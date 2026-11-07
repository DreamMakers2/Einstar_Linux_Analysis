#include <libusb.h>
#include <stdio.h>
#include <string.h>
int main(void){
  libusb_context *ctx=NULL; libusb_device **list=NULL;
  libusb_init(&ctx);
  ssize_t n=libusb_get_device_list(ctx,&list);
  libusb_device_handle *h=NULL;
  for(ssize_t i=0;i<n;i++){
    struct libusb_device_descriptor d;
    if(libusb_get_device_descriptor(list[i],&d)!=0) continue;
    if(d.idVendor==0x3267 && d.idProduct==0x0003){ libusb_open(list[i],&h); break; }
  }
  if(!h){printf("no device\n");return 2;}
  unsigned char buf[512];
  struct { int type; int len; const char *name; } reqs[] = {
    {0x06, 10, "device qualifier"},
    {0x0f, 255, "BOS"},
    {0x03, 255, "string 0"},
  };
  for(unsigned k=0;k<sizeof(reqs)/sizeof(reqs[0]);k++){
    memset(buf,0,sizeof buf);
    int rc=libusb_control_transfer(h,0x80,0x06,(unsigned short)(reqs[k].type<<8),0,buf,reqs[k].len,1000);
    printf("%s (type 0x%02x) -> rc=%d\n",reqs[k].name,reqs[k].type,rc);
    if(rc>0){for(int i=0;i<rc;i++)printf("%02x ",buf[i]);printf("\n");}
  }
  libusb_close(h); libusb_free_device_list(list,1); libusb_exit(ctx);
  return 0;
}
