#define _GNU_SOURCE
#include <stdatomic.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
struct str{unsigned char b[24];};
struct cb{atomic_int refs;uint32_t pad;void(*invoke)(struct cb*,const struct str*,const struct str*);void(*destroy)(struct cb*);int(*check)(void*,int);};
static atomic_int requests;
static _Thread_local int in_context;
int fixture_sdk_profile(char *account,size_t account_cap,char *name,size_t name_cap){
 const char *path=getenv("HELPER_FIXTURE_PROFILE_FILE");
 if(!path)return 0;
 FILE *input=fopen(path,"r");if(!input)return 0;
 char value[128]={0};if(!fgets(value,sizeof(value),input)){fclose(input);return 0;}fclose(input);
 value[strcspn(value,"\r\n")]=0;if(!value[0])return 0;
 snprintf(account,account_cap,"%s",value);snprintf(name,name_cap,"Fixture");return 1;
}
int fixture_sdk_context(void){return in_context;}
void fixture_sdk_string(struct str*out,const char*data,size_t n){memset(out,0,24);if(n<=22){out->b[0]=(unsigned char)(n*2);memcpy(out->b+1,data,n);}else{size_t cap=n+1;char*p=malloc(cap);memcpy(p,data,n);p[n]=0;cap|=1;memcpy(out->b,&cap,8);memcpy(out->b+8,&n,8);memcpy(out->b+16,&p,8);}}
void fixture_sdk_delete(void*p){free(p);}
static const char*raw(const struct str*s){if(s->b[0]&1){char*p;memcpy(&p,s->b+16,8);return p;}return (char*)s->b+1;}
struct task{struct cb*c;int send;int delay_us;};
static void*run(void*arg){struct task*t=arg;usleep(t->delay_us);struct str a={0},b={0};const char*reply=t->send==2?"{\"success\":false,\"error_message\":\"fixture rejected\"}":t->send?"{\"success\":true}":"";const char*error=t->send?"":"Parameter 'chat_id' is required and must be a string";fixture_sdk_string(&a,reply,strlen(reply));fixture_sdk_string(&b,error,strlen(error));t->c->invoke(t->c,&a,&b);if(a.b[0]&1)free((void*)raw(&a));if(b.b[0]&1)free((void*)raw(&b));if(atomic_fetch_sub(&t->c->refs,1)==1)t->c->destroy(t->c);free(t);return NULL;}
void fixture_sdk_send(void*x,const struct str*j,const struct str*r,struct cb**c){(void)x;if(!in_context)abort();if(strcmp(raw(r),"helper-linux-test"))abort();struct task*t=calloc(1,sizeof(*t));if(strstr(raw(j),"FORCE_EOF"))_exit(0);t->send=strstr(raw(j),"NATIVE_REJECT")?2:(strstr(raw(j),"\"chat_id\"")&&(strstr(raw(j),"\"content\"")||strstr(raw(j),"\"content_xml\"")))!=0;t->delay_us=(strstr(raw(j),"CALLBACK_DELAY")||strstr(raw(j),"LATE_CALLBACK_FIXTURE"))?180000:10000;t->c=*c;*c=NULL;atomic_fetch_add(&requests,1);pthread_t p;if(pthread_create(&p,NULL,run,t))abort();pthread_detach(p);}
void fixture_sdk_send_generic(void*x,const struct str*j,const struct str*r,struct cb**c){if(!strstr(raw(j),"message_type"))abort();fixture_sdk_send(x,j,r,c);}
struct function{_Alignas(16) unsigned char storage[32];void*callable;};
struct callable{void**v;char operation;};
struct shared{void*a,*b;};
static void*dispatch_run(void*p){struct callable*c=p;in_context=1;((void(*)(void*))c->v[6])(c);in_context=0;((void(*)(void*))c->v[5])(c);return NULL;}
void fixture_sdk_dispatch(struct shared*out,void*queue,const void*source,struct function*fn,int kind){(void)queue;(void)source;if(kind!=1)abort();struct callable*c=fn->callable;struct callable*copy=((struct callable*(*)(void*))c->v[2])(c);fn->callable=NULL;((void(*)(void*))c->v[5])(c);out->a=NULL;out->b=NULL;pthread_t t;if(pthread_create(&t,NULL,dispatch_run,copy))abort();pthread_detach(t);}
int fixture_sdk_media(char type,const char*target,const char*path){if(!in_context)abort();if(type!='I'&&type!='F')return 0;if(!target[0]||path[0]!='/')return 0;return 1;}
static unsigned char fixture_image_context[0x280],fixture_voice_context[0x280];
static int fixture_dto_voice;
static void fixture_image_destroy(void*p){(void)p;}
static void*fixture_image_vtable[]={NULL,NULL,fixture_image_destroy};
static struct {void**vtable;_Atomic int64_t refs;int64_t weak;} fixture_image_control={.vtable=fixture_image_vtable};
void fixture_sdk_weak_release(void*p){(void)p;}
void*fixture_image_dto_impl(void*out,void*source){(void)source;memset(out,0,0x350);uint32_t type=fixture_dto_voice?34:3;memcpy((char*)out+8,&type,4);struct shared value={fixture_dto_voice?fixture_voice_context:fixture_image_context,&fixture_image_control};memcpy((char*)out+0x238,&value,16);return out;}
__attribute__((naked,noinline,aligned(16)))void*fixture_image_dto(void*out __attribute__((unused)),void*source __attribute__((unused))){__asm__ __volatile__(".rept 15\n nop\n .endr\n jmp fixture_image_dto_impl");}
static void fixture_emit_image_context(const char*peer,const char*account,const char*md5){
 unsigned char source[0x280]={0},metadata[0x200]={0},output[0x350]={0};void*meta=metadata;
 fixture_sdk_string((struct str*)(source+0x18),peer,strlen(peer));fixture_sdk_string((struct str*)(source+0x30),account,strlen(account));fixture_sdk_string((struct str*)(metadata+0x1c8),md5,32);memcpy(source+0x220,&meta,8);
 atomic_store(&fixture_image_control.refs,0);fixture_image_dto(output,source);
 atomic_fetch_sub(&fixture_image_control.refs,1);
 for(int i=0;i<2;i++){struct str*value=(struct str*)(source+(i?0x30:0x18));if(value->b[0]&1)free((void*)raw(value));}
 free((void*)raw((struct str*)(metadata+0x1c8)));
}
void fixture_image_scope_ready(const char*peer,const char*account){fixture_emit_image_context("wxid_unscoped_fixture",account,"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");fixture_emit_image_context(peer,account,"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");}
void fixture_voice_scope_ready(const char*peer,const char*account){
 unsigned char source[0x280]={0},output[0x350]={0};
 fixture_sdk_string((struct str*)(source+0x18),peer,strlen(peer));fixture_sdk_string((struct str*)(source+0x30),account,strlen(account));
 fixture_dto_voice=1;fixture_image_dto(output,source);fixture_dto_voice=0;
 for(int i=0;i<2;i++){struct str*value=(struct str*)(source+(i?0x30:0x18));if(value->b[0]&1)free((void*)raw(value));}
}
int fixture_native_voice_download(const struct shared*voice,int kind,int scene,const void*options){
 uint32_t mode;memcpy(&mode,options,4);
 if(!in_context||voice->a!=fixture_voice_context||voice->b!=&fixture_image_control||kind!=11||scene!=0||mode!=1){abort();}
 return 1;
}
int fixture_native_image_download(const struct shared*image,int quality,int scene,const void*options){
 if(!in_context)abort();
 uint32_t mode;memcpy(&mode,options,4);
 if(image->a!=fixture_image_context||image->b!=&fixture_image_control||quality!=3||scene!=100||mode!=1)abort();
 return 1;
}
int main(void){sleep(30);return 0;}
