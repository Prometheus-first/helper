#define _GNU_SOURCE
/* Experimental Linux native text sender. Commands are serialized; no automatic retries. */
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <linux/if_alg.h>
#include <sys/socket.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <unistd.h>
#include <arpa/inet.h>

#include <pthread.h>
#include <sys/un.h>
#include <time.h>
#include <stddef.h>
#include <sys/time.h>
#include <sys/mman.h>
#include <inttypes.h>

static const unsigned char build_id[20]={0xd6,0x2b,0xde,0x21,0xb2,0x6c,0x59,0x28,0xe7,0xde,0xa8,0x26,0x27,0x1e,0x21,0xa5,0x42,0xb7,0x55,0xb5};
static const unsigned char exe_sha256[32]={0x27,0x49,0x53,0x5f,0x29,0x9b,0xa6,0x7f,0xeb,0x8e,0xf8,0x0c,0xab,0x89,0x8b,0xb0,0x53,0x1f,0xa8,0xf3,0x76,0xb1,0x6f,0x8a,0x72,0xdb,0x70,0x68,0x11,0xc8,0x41,0x32};
static int rd(void*out,const void*in,size_t n){struct iovec a={out,n},b={(void*)in,n};return process_vm_readv(getpid(),&a,1,&b,1,0)==(ssize_t)n;}
static int check_hash(void){
 int tfm=-1,op=-1,file=-1,ok=0;unsigned char buf[65536],digest[32];
 tfm=socket(AF_ALG,SOCK_SEQPACKET|SOCK_CLOEXEC,0);if(tfm<0)goto out;
 struct sockaddr_alg sa={.salg_family=AF_ALG,.salg_type="hash",.salg_name="sha256"};
 if(bind(tfm,(struct sockaddr*)&sa,sizeof(sa))<0)goto out;
 op=accept4(tfm,NULL,NULL,SOCK_CLOEXEC);if(op<0)goto out;
 file=open("/proc/self/exe",O_RDONLY|O_CLOEXEC);if(file<0)goto out;
 for(;;){ssize_t n=read(file,buf,sizeof(buf));if(n<0){if(errno==EINTR)continue;goto out;}if(n==0)break;size_t pos=0;while(pos<(size_t)n){ssize_t k=send(op,buf+pos,(size_t)n-pos,MSG_MORE);if(k<0){if(errno==EINTR)continue;goto out;}if(!k)goto out;pos+=(size_t)k;}}
 if(send(op,NULL,0,0)<0||read(op,digest,sizeof(digest))!=(ssize_t)sizeof(digest))goto out;
 ok=memcmp(digest,exe_sha256,32)==0;
 out:if(file>=0)close(file);if(op>=0)close(op);if(tfm>=0)close(tfm);return ok;
}
static int main_build(struct dl_phdr_info *info,size_t size,void *data){
 (void)size;if(info->dlpi_name&&info->dlpi_name[0])return 0;
 for(ElfW(Half) i=0;i<info->dlpi_phnum;i++){const ElfW(Phdr)*h=&info->dlpi_phdr[i];if(h->p_type!=PT_NOTE)continue;const unsigned char*p=(const unsigned char*)(info->dlpi_addr+h->p_vaddr),*end=p+h->p_memsz;
  while(p+sizeof(ElfW(Nhdr))<=end){const ElfW(Nhdr)*n=(const ElfW(Nhdr)*)p;p+=sizeof(*n);size_t names=(n->n_namesz+3u)&~3u,desc=(n->n_descsz+3u)&~3u;if(names>(size_t)(end-p)||desc>(size_t)(end-p)-names)break;if(n->n_type==NT_GNU_BUILD_ID&&n->n_namesz==4&&n->n_descsz==20&&!memcmp(p,"GNU",4)&&!memcmp(p+names,build_id,20)){*(uintptr_t*)data=info->dlpi_addr;return 1;}p+=names+desc;}}
 return 0;
}

struct sdk_string { unsigned char bytes[24]; };
struct sdk_callback { atomic_int refs; uint32_t pad; void (*invoke)(struct sdk_callback*,const struct sdk_string*,const struct sdk_string*); void (*destroy)(struct sdk_callback*); int (*check)(void*,int); };
_Static_assert(offsetof(struct sdk_callback,invoke)==8,"invoke ABI");
_Static_assert(offsetof(struct sdk_callback,destroy)==16,"destroy ABI");
_Static_assert(sizeof(struct sdk_string)==24,"string ABI");
typedef void (*sdk_entry)(void*,const struct sdk_string*,const struct sdk_string*,struct sdk_callback**);
typedef void (*sdk_ctor)(struct sdk_string*,const char*,size_t);
static sdk_entry sdk_send,sdk_send_generic;static sdk_ctor sdk_str;static void (*sdk_delete)(void*);
static atomic_int sdk_context_verified;
static int sdk_listener=-1;static pid_t sdk_parent;
static pthread_mutex_t sdk_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t sdk_cond=PTHREAD_COND_INITIALIZER;
static int sdk_done,sdk_running;static char sdk_result[8192],sdk_error[2048],sdk_path[108];
static size_t sdk_copy(char*out,size_t cap,const struct sdk_string*s){
 unsigned char h[24];if(!s||!rd(h,s,sizeof(h)))return 0;
 size_t n;uintptr_t p;if(h[0]&1){memcpy(&n,h+8,8);memcpy(&p,h+16,8);}else{n=h[0]>>1;p=(uintptr_t)s+1;if(n>22)return 0;}
 if(n>=cap||!cap||!rd(out,(void*)p,n))return 0;
 out[n]=0;return n;
}
static void sdk_cb_destroy(struct sdk_callback*c){free(c);}
static int sdk_cb_check(void*x,int v){(void)x;return v!=0;}
static void sdk_cb_invoke(struct sdk_callback*c,const struct sdk_string*a,const struct sdk_string*b){
 (void)c;pthread_mutex_lock(&sdk_mutex);
 sdk_copy(sdk_result,sizeof(sdk_result),a);sdk_copy(sdk_error,sizeof(sdk_error),b);sdk_done=1;
 pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
#ifndef SDK_FIXTURE
static uintptr_t sdk_base;
#endif
static char sdk_expected_account[128];
/* Fixed-layout read-only identity, validated against this exact ELF build. */
static int sdk_profile(char account[128],char name[512]){
 account[0]=name[0]=0;
#ifdef SDK_FIXTURE
 int(*profile)(char*,size_t,char*,size_t)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_profile");
 return profile?profile(account,128,name,512):0;
#else
 uintptr_t app=0,av=0,hub=0,hv=0,app2=0,hub2=0;unsigned char logged=0,logged2=0;
 if(!rd(&app,(void*)(sdk_base+0xacf6710),8)||!app||!rd(&av,(void*)app,8)||av!=sdk_base+0xa89cba8||
    !rd(&hub,(void*)(app+0x68),8)||!hub||!rd(&hv,(void*)hub,8)||hv!=sdk_base+0xa8aa750||
    !rd(&logged,(void*)(hub+0x38),1)||!(logged&1)||
    !sdk_copy(account,128,(struct sdk_string*)(hub+0x48)))return 0;
 for(size_t i=0;account[i];i++)if(!((account[i]>='a'&&account[i]<='z')||(account[i]>='A'&&account[i]<='Z')||
    (account[i]>='0'&&account[i]<='9')||strchr("_.@-",account[i]))){account[0]=0;return 0;}
 sdk_copy(name,512,(struct sdk_string*)(hub+0x78));
 char check[128]={0},name2[512]={0};
 if(!rd(&app2,(void*)(sdk_base+0xacf6710),8)||app2!=app||!rd(&hub2,(void*)(app+0x68),8)||hub2!=hub||
    !rd(&logged2,(void*)(hub+0x38),1)||!(logged2&1)||
    !sdk_copy(check,sizeof(check),(struct sdk_string*)(hub+0x48))||strcmp(account,check)){account[0]=0;return 0;}
 sdk_copy(name2,sizeof(name2),(struct sdk_string*)(hub+0x78));
 if(strcmp(name,name2))name[0]=0;
 return 1;
#endif
}
static void sdk_string_destroy(struct sdk_string*s){if(s->bytes[0]&1){void*p=NULL;memcpy(&p,s->bytes+16,8);sdk_delete(p);}memset(s,0,sizeof(*s));}
static void sdk_write(int fd,const char*s){size_t n=strlen(s);while(n){ssize_t k=send(fd,s,n,MSG_NOSIGNAL);if(k<0){if(errno==EINTR)continue;break;}if(!k)break;s+=k;n-=k;}}
static void sdk_escape(char*out,size_t cap,const char*s){size_t n=0;for(;*s&&n+7<cap;s++){unsigned char c=(unsigned char)*s;if(c=='"'||c=='\\'){out[n++]='\\';out[n++]=(char)c;}else if(c<32){int k=snprintf(out+n,cap-n,"\\u%04x",c);n+=(size_t)k;}else out[n++]=(char)c;}out[n]=0;}
struct sdk_function { _Alignas(16) unsigned char storage[32]; void *callable; };
struct sdk_payload { atomic_uint refs; char operation; char *json; };
struct sdk_callable { void **vtable; struct sdk_payload *payload; };
struct sdk_source { const char *file; const char *function; uint32_t line,pad; const void *pc; };
struct sdk_shared { void *object; void *control; };
static void sdk_finish_error(const char*msg){
 pthread_mutex_lock(&sdk_mutex);snprintf(sdk_error,sizeof(sdk_error),"%s",msg);sdk_result[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static int sdk_has_coroutine(void){
#ifdef SDK_FIXTURE
 int(*check)(void)=(int(*)(void))dlsym(RTLD_DEFAULT,"fixture_sdk_context");return check&&check();
#else
 uintptr_t thread=(uintptr_t)__builtin_thread_pointer(),ptr=0,co=0;
 return rd(&ptr,(void*)(thread-0x78),8)&&ptr&&rd(&co,(void*)ptr,8)&&co;
#endif
}
static void sdk_release(struct sdk_shared*s);
static void sdk_finish_success(void){pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);}
struct native_media_status {uint32_t error_type,error_code;struct sdk_string error;void *object,*control;};
_Static_assert(sizeof(struct native_media_status)==48,"send result ABI");
static int unhex(char c){if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;return -1;}
/* Python fully decodes a bounded private GIF snapshot. Recheck the native
 * boundary too, including raw O frames; never classify another file as type 47. */
static int sdk_emoticon_file(const char*path){
 int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);if(fd<0)return 0;
 struct stat st;unsigned char head[10]={0},tail=0;
 int ok=!fstat(fd,&st)&&S_ISREG(st.st_mode)&&st.st_nlink==1&&st.st_uid==geteuid()&&
  !(st.st_mode&0077)&&st.st_size>=14&&st.st_size<=1048576&&
  pread(fd,head,sizeof(head),0)==(ssize_t)sizeof(head)&&pread(fd,&tail,1,st.st_size-1)==1&&
  (!memcmp(head,"GIF87a",6)||!memcmp(head,"GIF89a",6))&&tail==0x3b;
 unsigned width=head[6]|((unsigned)head[7]<<8),height=head[8]|((unsigned)head[9]<<8);
 close(fd);return ok&&width&&width<=1024&&height&&height<=1024;
}
static void sdk_perform_media(char operation,const char*payload){
 const char*sep=strchr(payload,'\n');if(!sep||sep==payload||sep-payload>127){sdk_finish_error("invalid media target");return;}
 size_t tn=(size_t)(sep-payload),hexlen=strlen(sep+1);if(!hexlen||(hexlen&1)||(operation=='O'&&hexlen>2046)){sdk_finish_error("invalid media path frame");return;}
 char target[128];memcpy(target,payload,tn);target[tn]=0;
 for(size_t i=0;i<tn;i++)if(!((target[i]>='a'&&target[i]<='z')||(target[i]>='A'&&target[i]<='Z')||(target[i]>='0'&&target[i]<='9')||strchr("_.@-",target[i]))){sdk_finish_error("invalid media target");return;}
 char*path=malloc(hexlen/2+1);if(!path){sdk_finish_error("allocation failed");return;}
 for(size_t i=0;i<hexlen;i+=2){int a=unhex(sep[1+i]),b=unhex(sep[2+i]);if(a<0||b<0||(!a&&!b)){free(path);sdk_finish_error("invalid media path encoding");return;}path[i/2]=(char)((a<<4)|b);}path[hexlen/2]=0;
 struct stat st;if(path[0]!='/'||stat(path,&st)||!S_ISREG(st.st_mode)||st.st_size<=0){free(path);sdk_finish_error("media file missing or empty");return;}
 if(operation=='O'&&!sdk_emoticon_file(path)){free(path);sdk_finish_error("emoticon requires a private bounded GIF snapshot");return;}
#ifdef SDK_FIXTURE
 int(*send)(char,const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_media");
 int result=send?send(operation,target,path):-1;free(path);
 if(result==1)sdk_finish_success();else sdk_finish_error("fixture media send rejected");
#else
 struct sdk_string to={0};unsigned char native_path[24]={0};
 struct sdk_shared hub={0},center={0},manager={0},message={0};struct native_media_status status={0};
 sdk_str(&to,target,tn);
 ((void(*)(void*,const char*,size_t))(sdk_base+0x453c040))(native_path,path,hexlen/2);free(path);
 /* 0x6f803b0 classifies the local path; 0x6f8054c constructs the type-47 DTO.
  * 0x6f809c6 copies image_info+24 into DTO+0x5e8, then assigns path and target.
  * Its dispatched lambda 0x6f91ed0 calls this same manager at 0x6f91fe3.
  * manager -> 0x64d6ba0 waits at 0x64d6bd0 before returning the status. */
 unsigned char image_info[32]={0};int emoticon_ready=1;
 if(operation=='O'){
  emoticon_ready=((int(*)(const void*))(sdk_base+0x73dc1f0))(native_path)==2;
  if(emoticon_ready){((void(*)(void*,const void*))(sdk_base+0x7592ae0))(image_info,native_path);emoticon_ready=image_info[0]!=0;}
 }
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int invoked=0;
 if(manager.object&&emoticon_ready){
  unsigned char scratch[64]={0};
  ((void(*)(struct sdk_shared*,void*))(sdk_base+(operation=='O'?0x496bb00:operation=='I'?0x4a03f10:0x5325050)))(&message,scratch);
  uintptr_t vtable=0;
  if(message.object&&(operation!='O'||(rd(&vtable,message.object,8)&&vtable==sdk_base+0xa8a11b8))){
   uint32_t type=operation=='O'?47:operation=='I'?3:49,subtype=operation=='F'?6:0;
   memcpy((char*)message.object+0xe4,&type,4);memcpy((char*)message.object+0xe8,&subtype,4);
   if(operation=='O')memcpy((char*)message.object+0x5e8,image_info+24,8);
   ((void(*)(void*,const struct sdk_string*))(sdk_base+0x450fdb0))((char*)message.object+0x90,&to);
   ((void(*)(void*,const void*))(sdk_base+0x45396a0))((char*)message.object+0xf0,native_path);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);invoked=1;
  }
 }
 if(invoked&&!status.error_type&&!status.error_code)sdk_finish_success();
 else if(invoked){char error[1024]={0};sdk_copy(error,sizeof(error),&status.error);sdk_finish_error(error[0]?error:"native media send failed");}
 else sdk_finish_error(emoticon_ready?"native message manager or DTO unavailable":"native emoticon validation failed");
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 if(operation=='O')((void(*)(void*))(sdk_base+0x939bf60))(image_info+8);
 ((void(*)(void*))(sdk_base+0x4539690))(native_path);sdk_string_destroy(&to);
#endif
}
/* Text message layout, exact 4.1.13.26 build: ctor 0x4a040a0;
 * 0x60ace38 / 0x60ace8a register content +0x5c8 and at_user_list +0x5e0.
 * The latter property resolves via vtable 0xa8a3448 to 0x60bab90.
 * Plain SDK send_text ignores mention metadata; construct the native text DTO. */
static int sdk_identifier(const char *value,size_t n){
 if(!n||n>127)return 0;
 for(size_t i=0;i<n;i++)if(!((value[i]>='a'&&value[i]<='z')||(value[i]>='A'&&value[i]<='Z')||
    (value[i]>='0'&&value[i]<='9')||strchr("_.@-",value[i])))return 0;
 return 1;
}
/* 4.1.13.26 voice send: 0x810b1a0 -> 0x810c840 -> 0x810c9a0.
 * The ctor deep-copies recipient, SILK bytes (+0x5c8), duration (+0x5e0)
 * and options. 0x69f6cfa copies those bytes into the upload buffer; it does
 * not open a path. 0x60c3d20/47 serialize duration/format into voice XML.
 * Match the recording UI's {4,3,30000,0,empty string} options at 0x4a18ea8.
 * Manager completion is a native stage result, not proof of delivery. */
struct sdk_voice_options {uint32_t format,mode;uint64_t timeout;unsigned char flag,pad[7];struct sdk_string extra;};
_Static_assert(sizeof(struct sdk_voice_options)==48,"voice options ABI");
_Static_assert(offsetof(struct sdk_voice_options,extra)==24,"voice options string ABI");
static unsigned char*sdk_voice_file(const char*path,uint32_t duration,size_t*length){
 int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);if(fd<0)return NULL;
 struct stat before,after;unsigned char*data=NULL;size_t n=0,done=0;
 if(fstat(fd,&before)||!S_ISREG(before.st_mode)||before.st_nlink!=1||before.st_uid!=geteuid()||
    (before.st_mode&0077)||before.st_size<13||before.st_size>262144)goto out;
 n=(size_t)before.st_size;data=malloc(n);if(!data)goto out;
 while(done<n){ssize_t k=read(fd,data+done,n-done);if(k<0&&errno==EINTR)continue;if(k<=0)goto bad;done+=(size_t)k;}
 if(fstat(fd,&after)||before.st_size!=after.st_size||before.st_mtim.tv_sec!=after.st_mtim.tv_sec||
    before.st_mtim.tv_nsec!=after.st_mtim.tv_nsec||before.st_ctim.tv_sec!=after.st_ctim.tv_sec||
    before.st_ctim.tv_nsec!=after.st_ctim.tv_nsec||memcmp(data,"\x02#!SILK_V3",10))goto bad;
 size_t pos=10;uint32_t packets=0;
 while(pos<n){
  if(n-pos<2)goto bad;
  unsigned size=(unsigned)data[pos]|((unsigned)data[pos+1]<<8);pos+=2;
  if(!size||size>32767||size>n-pos||++packets>3000)goto bad;
  pos+=size;
 }
 if(!packets||packets*20!=duration)goto bad;
 *length=n;close(fd);return data;
 bad:free(data);data=NULL;
 out:close(fd);return NULL;
}
static void sdk_perform_voice(const char*payload){
 const char*first=strchr(payload,'\n'),*second=first?strchr(first+1,'\n'):NULL;
 if(!first||!second||!sdk_identifier(payload,(size_t)(first-payload))||second==first+1||second-first>6){sdk_finish_error("invalid voice target or duration");return;}
 uint32_t duration=0;for(const char*p=first+1;p<second;p++){if(*p<'0'||*p>'9'){sdk_finish_error("invalid voice duration");return;}duration=duration*10+(unsigned)(*p-'0');}
 size_t hexlen=strlen(second+1);if(!duration||duration>60000||duration%20||!hexlen||(hexlen&1)||hexlen>2046){sdk_finish_error("invalid voice duration or path");return;}
 char target[128],path[1024];size_t tn=(size_t)(first-payload);memcpy(target,payload,tn);target[tn]=0;
 for(size_t i=0;i<hexlen;i+=2){int a=unhex(second[1+i]),b=unhex(second[2+i]);if(a<0||b<0||(!a&&!b)){sdk_finish_error("invalid voice path encoding");return;}path[i/2]=(char)((a<<4)|b);}path[hexlen/2]=0;
 size_t size=0;unsigned char*bytes=path[0]=='/'?sdk_voice_file(path,duration,&size):NULL;
 if(!bytes){sdk_finish_error("voice requires private complete Tencent SILK matching duration (max 262144 bytes)");return;}
 char account[128],name[512];if(!sdk_expected_account[0]||!sdk_profile(account,name)||strcmp(account,sdk_expected_account)){free(bytes);sdk_finish_error("WeChat account changed; refusing stale voice send");return;}
#ifdef SDK_FIXTURE
 int(*send_voice)(const char*,const unsigned char*,size_t,uint32_t)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_voice");
 int ok=send_voice?send_voice(target,bytes,size,duration):0;free(bytes);
 if(ok)sdk_finish_success();else sdk_finish_error("fixture voice send rejected");
#else
 struct sdk_string to={0},audio={0};struct sdk_voice_options options={.format=4,.mode=3,.timeout=30000};
 struct sdk_shared hub={0},center={0},manager={0},message={0};struct native_media_status status={0};
 sdk_str(&to,target,tn);sdk_str(&audio,(const char*)bytes,size);free(bytes);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int invoked=0;
 if(manager.object){
  unsigned char scratch[16]={0};
  ((void(*)(struct sdk_shared*,void*,const struct sdk_string*,const struct sdk_string*,const uint32_t*,const struct sdk_voice_options*))(sdk_base+0x810c840))(&message,scratch,&to,&audio,&duration,&options);
  uintptr_t vtable=0;
  if(message.object&&rd(&vtable,message.object,8)&&vtable==sdk_base+0xa9dae60&&sdk_profile(account,name)&&!strcmp(account,sdk_expected_account)){
   uint32_t type=34;memcpy((char*)message.object+0xe4,&type,4);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);invoked=1;
  }
 }
 if(invoked&&!status.error_type&&!status.error_code)sdk_finish_success();
 else if(invoked){char error[1024]={0};sdk_copy(error,sizeof(error),&status.error);sdk_finish_error(error[0]?error:"native voice send failed");}
 else sdk_finish_error("native voice manager, DTO or account unavailable");
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 sdk_string_destroy(&audio);sdk_string_destroy(&to);
#endif
}
/* Video DTO layout from the fingerprinted 4.1.13.26 executable. */
static void sdk_perform_video(const char*payload){
 const char*first=strchr(payload,'\n'),*second=first?strchr(first+1,'\n'):NULL;
 if(!first||!second||!sdk_identifier(payload,(size_t)(first-payload))||
    second==first+1||second-first>11){sdk_finish_error("invalid video target or duration");return;}
 uint64_t duration=0;
 for(const char*p=first+1;p<second;p++){
  if(*p<'0'||*p>'9'||duration>214748364){sdk_finish_error("invalid video duration");return;}
  duration=duration*10+(unsigned)(*p-'0');
 }
 size_t hexlen=strlen(second+1);
 if(!duration||duration>2147483647||!hexlen||(hexlen&1)||hexlen>2046){sdk_finish_error("invalid video duration or path");return;}
 char target[128],path[1024];size_t tn=(size_t)(first-payload);memcpy(target,payload,tn);target[tn]=0;
 for(size_t i=0;i<hexlen;i+=2){int a=unhex(second[1+i]),b=unhex(second[2+i]);if(a<0||b<0||(!a&&!b)){sdk_finish_error("invalid video path encoding");return;}path[i/2]=(char)((a<<4)|b);}path[hexlen/2]=0;
 size_t pathlen=hexlen/2;
 if(path[0]!='/'||pathlen<5||strcasecmp(path+pathlen-4,".mp4")){sdk_finish_error("video path must end in .mp4");return;}
 int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);struct stat st;unsigned char header[12];
 int valid=fd>=0&&!fstat(fd,&st)&&S_ISREG(st.st_mode)&&st.st_uid==geteuid()&&st.st_nlink==1&&
  !(st.st_mode&0077)&&st.st_size>12&&st.st_size<=67108864&&pread(fd,header,sizeof(header),0)==(ssize_t)sizeof(header)&&!memcmp(header+4,"ftyp",4);
 if(fd>=0)close(fd);
 if(!valid){sdk_finish_error("video requires a private MP4 snapshot");return;}
 char account[128],name[512];if(!sdk_expected_account[0]||!sdk_profile(account,name)||strcmp(account,sdk_expected_account)){
  sdk_finish_error("WeChat account changed; refusing stale video send");return;
 }
#ifdef SDK_FIXTURE
 int(*send_video)(const char*,const char*,uint32_t)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_video");
 if(send_video&&send_video(target,path,(uint32_t)duration)==1)sdk_finish_success();else sdk_finish_error("fixture video send rejected");
#else
 struct sdk_string to={0},filename={0};unsigned char native_path[24]={0};
 struct sdk_shared hub={0},center={0},manager={0},message={0};struct native_media_status status={0};
 const char*basename=strrchr(path,'/');basename=basename?basename+1:path;
 sdk_str(&to,target,tn);sdk_str(&filename,basename,strlen(basename));
 ((void(*)(void*,const char*,size_t))(sdk_base+0x453c040))(native_path,path,pathlen);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int invoked=0;
 if(manager.object){
  unsigned char scratch[64]={0};
  ((void(*)(struct sdk_shared*,void*))(sdk_base+0x53fb010))(&message,scratch);
  uintptr_t vtable=0;
  if(message.object&&rd(&vtable,message.object,8)&&vtable==sdk_base+0xa8a1168&&sdk_profile(account,name)&&!strcmp(account,sdk_expected_account)){
   uint32_t type=43,seconds=(uint32_t)duration;uint64_t bytes=(uint64_t)st.st_size;
   memcpy((char*)message.object+0xe4,&type,4);memcpy((char*)message.object+0x5dc,&seconds,4);memcpy((char*)message.object+0x170,&bytes,8);
   ((void(*)(void*,const struct sdk_string*))(sdk_base+0x450fdb0))((char*)message.object+0x90,&to);
   ((void(*)(void*,const struct sdk_string*))(sdk_base+0x450fdb0))((char*)message.object+0x158,&filename);
   ((void(*)(void*,const void*))(sdk_base+0x45396a0))((char*)message.object+0xf0,native_path);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);invoked=1;
  }
 }
 if(invoked&&!status.error_type&&!status.error_code)sdk_finish_success();
 else if(invoked){char error[1024]={0};sdk_copy(error,sizeof(error),&status.error);sdk_finish_error(error[0]?error:"native video send failed");}
 else sdk_finish_error("native video manager, DTO or account unavailable");
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 ((void(*)(void*))(sdk_base+0x4539690))(native_path);sdk_string_destroy(&filename);sdk_string_destroy(&to);
#endif
}
static void sdk_perform_mention(const char *payload){
 const char *first=strchr(payload,'\n'),*second=first?strchr(first+1,'\n'):NULL;
 if(!first||!second){sdk_finish_error("invalid mention frame");return;}
 size_t tn=(size_t)(first-payload),mn=(size_t)(second-first-1),cn=strlen(second+1);
 if(!sdk_identifier(payload,tn)||tn<=9||memcmp(payload+tn-9,"@chatroom",9)||!mn||mn>1023||!cn||cn>1023){
  sdk_finish_error("invalid mention target or length");return;
 }
 char target[128],members[1024],check[1024];memcpy(target,payload,tn);target[tn]=0;
 memcpy(members,first+1,mn);members[mn]=0;memcpy(check,members,mn+1);
 char *ids[8],*part=check;size_t count=0;
 for(;;){
  char *comma=strchr(part,',');if(comma)*comma=0;
  size_t n=strlen(part);
  if(count==8||!sdk_identifier(part,n)||(n>=9&&!strcmp(part+n-9,"@chatroom"))||
     !strncmp(part,"gh_",3)||!strcmp(part,"filehelper")||!strcmp(part,"weixin")||
     !strcmp(part,"fmessage")||!strcmp(part,"newsapp")||!strcmp(part,"floatbottle")||!strcmp(part,"notify@all")){
   sdk_finish_error("invalid mention member");return;
  }
  for(size_t i=0;i<count;i++)if(!strcmp(ids[i],part)){sdk_finish_error("duplicate mention member");return;}
  ids[count++]=part;if(!comma)break;part=comma+1;
 }
#ifdef SDK_FIXTURE
 int(*send)(const char*,const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_mention");
 if(send&&send(target,second+1,members)==1)sdk_finish_success();else sdk_finish_error("fixture mention rejected");
#else
 struct sdk_shared hub={0},center={0},manager={0},message={0};struct native_media_status status={0};
 struct sdk_string to={0},content={0},at={0};int invoked=0;
 sdk_str(&to,target,tn);sdk_str(&content,second+1,cn);sdk_str(&at,members,mn);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 if(manager.object){
  unsigned char scratch[64]={0};
  ((void(*)(struct sdk_shared*,void*))(sdk_base+0x4a040a0))(&message,scratch);
  uintptr_t vtable=0;
  if(message.object&&rd(&vtable,message.object,8)&&vtable==sdk_base+0xa8a10c8){
   uint32_t type=1;memcpy((char*)message.object+0xe4,&type,4);
   void(*assign)(void*,const struct sdk_string*)=(void*)(sdk_base+0x450fdb0);
   assign((char*)message.object+0x90,&to);assign((char*)message.object+0x5c8,&content);
   assign((char*)message.object+0x5e0,&at);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);
   invoked=1;
  }
 }
 if(invoked&&!status.error_type&&!status.error_code)sdk_finish_success();
 else sdk_finish_error(invoked?"native mention send failed":"native text message unavailable");
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 sdk_string_destroy(&at);sdk_string_destroy(&content);sdk_string_destroy(&to);
#endif
}
/* Retain the exact message context exported by the native DTO constructor. */
typedef void*(*sdk_image_dto_fn)(void*,void*);
static sdk_image_dto_fn sdk_original_image_dto;
static int sdk_download_hook_ready;
static int sdk_quote_ready;
/* The quote probe owns a native DTO copy. The receive callback's source pointer
 * and DTO+0x238 shared object are not used as lifetime guarantees. */
struct quote_source {
 _Alignas(16) unsigned char dto[0x360];
 uint64_t server_id;
 char peer[128];
};
#define QUOTE_SOURCE_LIMIT 16
/* The quote cache belongs to the same incoming conversation generation as
 * the media context and is guarded by sdk_image_mutex. */
static struct quote_source *quote_sources[QUOTE_SOURCE_LIMIT];
static unsigned quote_next;
#ifndef SDK_FIXTURE
static int sdk_parse_generation(const char *text,size_t n,uint64_t *result);
#endif
static void quote_destroy(struct quote_source *source){
 if(!source)return;
#ifndef SDK_FIXTURE
 ((void(*)(void*))(sdk_base+0x472f190))(source->dto);
#endif
 free(source);
}
#ifndef SDK_FIXTURE
static int sdk_quote_peer(const char *peer){
 return sdk_identifier(peer,strlen(peer))&&!strchr(peer,'@')&&strncmp(peer,"gh_",3)&&
  strcmp(peer,"filehelper")&&strcmp(peer,"weixin")&&strcmp(peer,"fmessage")&&
  strcmp(peer,"newsapp")&&strcmp(peer,"floatbottle");
}
#endif
static atomic_int sdk_image_capture_enabled,sdk_voice_capture_enabled;
static pthread_mutex_t sdk_image_mutex=PTHREAD_MUTEX_INITIALIZER;
static uint64_t sdk_image_generation;
static int sdk_image_multi_scope;
#ifndef SDK_FIXTURE
static int sdk_image_matches(const char *peer,const char *account);
static int sdk_image_scope_current(uint64_t generation);
#endif
static void quote_capture(void *dto,void *source){
#ifndef SDK_FIXTURE
 if(!atomic_load(&sdk_image_capture_enabled))return;
 uint64_t server_id=0, copied_id=0, type=0;
 uintptr_t vtable=0;
 char peer[128], account[128];
 if(!rd(&type,(char*)dto+8,8)||type!=1||
    !rd(&vtable,dto,8)||vtable!=sdk_base+0xa89f2f0||
    !sdk_copy(peer,sizeof(peer),(struct sdk_string*)((char*)dto+0x10))||
    !sdk_copy(account,sizeof(account),(struct sdk_string*)((char*)dto+0x28))||
    !sdk_quote_peer(peer)||!strcmp(peer,account)||
    !rd(&server_id,(char*)source+0xf8,8)||!server_id||
    !rd(&copied_id,(char*)dto+0x90,8)||server_id!=copied_id)return;
 struct quote_source *previous=NULL;
 pthread_mutex_lock(&sdk_image_mutex);
 if(sdk_image_multi_scope&&sdk_image_matches(peer,account)){
  for(unsigned i=0;i<QUOTE_SOURCE_LIMIT;i++){
   if(quote_sources[i]&&quote_sources[i]->server_id==server_id&&!strcmp(quote_sources[i]->peer,peer)){
    pthread_mutex_unlock(&sdk_image_mutex);
    return;
   }
  }
  struct quote_source *copy=calloc(1,sizeof(*copy));
  if(copy){
   ((void(*)(void*,const void*))(sdk_base+0x472e700))(copy->dto,dto);
   copy->dto[0x350]=1;
   copy->server_id=server_id;
   strcpy(copy->peer,peer);
   unsigned slot=quote_next++%QUOTE_SOURCE_LIMIT;
   previous=quote_sources[slot];
   quote_sources[slot]=copy;
  }
 }
 pthread_mutex_unlock(&sdk_image_mutex);
 quote_destroy(previous);
#else
 (void)dto;(void)source;
#endif
}
static void sdk_perform_quote(const char *payload){
#ifndef SDK_FIXTURE
 const char *scope_end=strchr(payload,'\n');
 const char *peer_end=scope_end?strchr(scope_end+1,'\n'):NULL;
 const char *id_end=peer_end?strchr(peer_end+1,'\n'):NULL;
 uint64_t generation=0,id=0;
 if(!id_end||!sdk_parse_generation(payload,(size_t)(scope_end-payload),&generation)||
    !sdk_identifier(scope_end+1,(size_t)(peer_end-scope_end-1))||
    !sdk_parse_generation(peer_end+1,(size_t)(id_end-peer_end-1),&id)||
    !id_end[1]||strlen(id_end+1)>1023){
  sdk_finish_error("invalid quote request");return;
 }
 char peer[128],account[128],name[512];
 size_t peer_size=(size_t)(peer_end-scope_end-1);
 memcpy(peer,scope_end+1,peer_size);peer[peer_size]=0;
 if(!sdk_quote_peer(peer)||!sdk_image_scope_current(generation)||!sdk_expected_account[0]||
    !sdk_profile(account,name)||strcmp(account,sdk_expected_account)||!strcmp(peer,account)){
  sdk_finish_error("quote scope or account changed");return;
 }
 pthread_mutex_lock(&sdk_image_mutex);
 struct quote_source *copy=NULL;
 if(generation==sdk_image_generation&&sdk_image_multi_scope&&sdk_image_matches(peer,account)){
  for(unsigned i=0;i<QUOTE_SOURCE_LIMIT;i++){
   struct quote_source *source=quote_sources[i];
   if(!source||source->server_id!=id||strcmp(peer,source->peer))continue;
   copy=calloc(1,sizeof(*copy));
   if(copy){
    ((void(*)(void*,const void*))(sdk_base+0x472e700))(copy->dto,source->dto);
    copy->dto[0x350]=1;
   }
   break;
  }
 }
 pthread_mutex_unlock(&sdk_image_mutex);
 if(!copy){sdk_finish_error("matching quote source not captured");return;}
 uint64_t copied_id=0;
 memcpy(&copied_id,copy->dto+0x90,8);
 if(copied_id!=id){quote_destroy(copy);sdk_finish_error("quote source identifier changed");return;}
 struct sdk_string content={0},to={0};
 struct sdk_shared message={0},hub={0},center={0},manager={0};
 struct native_media_status status={0};
 /* The native extra argument contains three libc++ strings and a scalar. */
 _Alignas(16) unsigned char extra[0x58]={0};
 sdk_str(&content,id_end+1,strlen(id_end+1));
 ((void(*)(struct sdk_shared*,void*,const struct sdk_string*,void*))(sdk_base+0x738fec0))(&message,copy->dto,&content,extra);
 quote_destroy(copy);
 uint64_t type=0,quoted_id=0;unsigned char quote_present=0;
 int built=message.object&&message.control&&rd(&type,(char*)message.object+0xe4,8)&&
  type==((UINT64_C(57)<<32)|49)&&rd(&quote_present,(char*)message.object+0x528,1)&&quote_present==1&&
  rd(&quoted_id,(char*)message.object+0x268,8)&&quoted_id==id;
 int invoked=0;
 if(built){
  ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
  if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
  if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
  if(manager.object&&sdk_image_scope_current(generation)&&sdk_profile(account,name)&&!strcmp(account,sdk_expected_account)){
   sdk_str(&to,peer,strlen(peer));
   ((void(*)(void*,const struct sdk_string*))(sdk_base+0x450fdb0))((char*)message.object+0x90,&to);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);
   invoked=1;
  }
 }
 int success=invoked&&!status.error_type&&!status.error_code;
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 sdk_string_destroy(&to);sdk_string_destroy(&content);
 if(success)sdk_finish_success();else sdk_finish_error(built?"native quote send failed":"native quote construction failed");
#else
 (void)payload;sdk_finish_error("native quote unavailable in fixture");
#endif
}
static struct sdk_shared sdk_voice_context;
static atomic_uint sdk_image_scoped_seen,sdk_image_cached_total;
static char sdk_image_peers[64][128],sdk_image_account[128];
static unsigned sdk_image_peer_count;
struct sdk_image_context {char md5[33];struct sdk_shared value;};
static struct sdk_image_context sdk_images[16];static unsigned sdk_image_next;
static int sdk_md5_valid(const char*value){if(strlen(value)!=32)return 0;for(int i=0;i<32;i++)if(unhex(value[i])<0)return 0;return 1;}
static void sdk_shared_retain(struct sdk_shared*value){if(value->control)__atomic_add_fetch((int64_t*)((char*)value->control+8),1,__ATOMIC_SEQ_CST);}
/* Caller holds sdk_image_mutex. source+0x18 must be in the selected scope;
 * source+0x30 must equal this account.
 * These fields copy to DTO+0x10/+0x28 at 0x605ea7d/0x605ea8a. */
static int sdk_scope_target(const char*value);
static int sdk_image_matches(const char*peer,const char*account){
 if(!atomic_load(&sdk_image_capture_enabled)||strcmp(account,sdk_image_account))return 0;
 for(unsigned i=0;i<sdk_image_peer_count;i++){
  if(!strcmp(peer,sdk_image_peers[i]))return 1;
  /* Empty application allowlist permits regular peers for this account only. */
  if(!strcmp(sdk_image_peers[i],"*")&&sdk_scope_target(peer)&&
     strcmp(peer,"filehelper")&&strcmp(peer,"weixin")&&strcmp(peer,"fmessage")&&
     strcmp(peer,"newsapp")&&strcmp(peer,"floatbottle")&&strncmp(peer,"gh_",3))return 1;
 }
 return 0;
}
static int sdk_clear_image_scope(uint64_t expected,int force){
 struct quote_source *old_quotes[QUOTE_SOURCE_LIMIT];
 struct sdk_shared old[16],voice={0};pthread_mutex_lock(&sdk_image_mutex);
 if(!force&&expected!=sdk_image_generation){pthread_mutex_unlock(&sdk_image_mutex);return 0;}
 atomic_store(&sdk_image_capture_enabled,0);atomic_store(&sdk_voice_capture_enabled,0);
 voice=sdk_voice_context;memset(&sdk_voice_context,0,sizeof(sdk_voice_context));
 for(unsigned i=0;i<16;i++){old[i]=sdk_images[i].value;memset(&sdk_images[i],0,sizeof(sdk_images[i]));}
 memcpy(old_quotes,quote_sources,sizeof(old_quotes));memset(quote_sources,0,sizeof(quote_sources));quote_next=0;
 memset(sdk_image_peers,0,sizeof(sdk_image_peers));sdk_image_peer_count=0;sdk_image_account[0]=0;
 sdk_image_next=0;sdk_image_multi_scope=0;sdk_image_generation++;
 atomic_store(&sdk_image_scoped_seen,0);atomic_store(&sdk_image_cached_total,0);pthread_mutex_unlock(&sdk_image_mutex);
 for(unsigned i=0;i<16;i++)sdk_release(&old[i]);
 for(unsigned i=0;i<QUOTE_SOURCE_LIMIT;i++)quote_destroy(old_quotes[i]);
 sdk_release(&voice);return 1;
}
static int sdk_image_scope_current(uint64_t generation){
 char account[128],current[128],name[512];pthread_mutex_lock(&sdk_image_mutex);
 int active=atomic_load(&sdk_image_capture_enabled)&&generation==sdk_image_generation;
 strcpy(account,sdk_image_account);pthread_mutex_unlock(&sdk_image_mutex);
 if(!active)return 0;
 if(!sdk_profile(current,name)||strcmp(current,account)){sdk_clear_image_scope(generation,0);return 0;}
 pthread_mutex_lock(&sdk_image_mutex);active=atomic_load(&sdk_image_capture_enabled)&&generation==sdk_image_generation;
 pthread_mutex_unlock(&sdk_image_mutex);return active;
}
static void*sdk_capture_image_dto(void*output,void*source){
 void*result=sdk_original_image_dto(output,source);
 quote_capture(output,source);
 uint32_t type=0;if(!atomic_load(&sdk_image_capture_enabled)||!rd(&type,(char*)output+8,4)||(type!=3&&(!atomic_load(&sdk_voice_capture_enabled)||type!=34)))return result;
 char peer[128],account[128],md5[33];
 if(!sdk_copy(peer,sizeof(peer),(struct sdk_string*)((char*)source+0x18))||!sdk_copy(account,sizeof(account),(struct sdk_string*)((char*)source+0x30)))return result;
 pthread_mutex_lock(&sdk_image_mutex);uint64_t generation=sdk_image_generation;int scoped=sdk_image_matches(peer,account);pthread_mutex_unlock(&sdk_image_mutex);
 if(!scoped||!sdk_image_scope_current(generation))return result;
 if(type==34){
  struct sdk_shared voice={0},previous={0};
  if(!rd(&voice,(char*)output+0x238,sizeof(voice))||!voice.object||!voice.control)return result;
  sdk_shared_retain(&voice);pthread_mutex_lock(&sdk_image_mutex);
  if(!atomic_load(&sdk_voice_capture_enabled)||generation!=sdk_image_generation||!sdk_image_matches(peer,account)||sdk_voice_context.object){pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&voice);return result;}
  previous=sdk_voice_context;sdk_voice_context=voice;pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&previous);return result;
 }
 atomic_fetch_add(&sdk_image_scoped_seen,1);
 void*metadata=NULL;struct sdk_shared context={0};
 if(!rd(&metadata,(char*)source+0x220,8)||!metadata||sdk_copy(md5,sizeof(md5),(struct sdk_string*)((char*)metadata+0x1c8))!=32||!sdk_md5_valid(md5)||!rd(&context,(char*)output+0x238,sizeof(context))||!context.object||!context.control)return result;
 sdk_shared_retain(&context);struct sdk_shared previous={0};
 pthread_mutex_lock(&sdk_image_mutex);
 if(generation!=sdk_image_generation||!sdk_image_matches(peer,account)){pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&context);return result;}
 unsigned slot=sdk_image_next++%16;for(unsigned i=0;i<16;i++)if(!strcmp(sdk_images[i].md5,md5)){slot=i;break;}
 previous=sdk_images[slot].value;strcpy(sdk_images[slot].md5,md5);sdk_images[slot].value=context;atomic_fetch_add(&sdk_image_cached_total,1);
 pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&previous);return result;
}
static void sdk_absolute_jump(unsigned char*out,const void*target){out[0]=0xff;out[1]=0x25;memset(out+2,0,4);memcpy(out+6,&target,8);}
static int sdk_install_image_context_capture(void){
 unsigned char*entry;
#ifdef SDK_FIXTURE
 entry=(unsigned char*)dlsym(RTLD_DEFAULT,"fixture_image_dto");if(!entry)return 0;
 static const unsigned char endbr[4]={0xf3,0x0f,0x1e,0xfa};if(!memcmp(entry,endbr,4))entry+=4;
 for(int i=0;i<15;i++)if(entry[i]!=0x90)return 0;
#else
 entry=(unsigned char*)(sdk_base+0x605e8a0);
 static const unsigned char expected[15]={0x55,0x41,0x57,0x41,0x56,0x41,0x54,0x53,0x48,0x83,0xec,0x30,0x49,0x89,0xf6};
 if(memcmp(entry,expected,15))return 0;
#endif
 long page=sysconf(_SC_PAGESIZE);if(page<=0)return 0;
 unsigned char*trampoline=mmap(NULL,(size_t)page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(trampoline==MAP_FAILED)return 0;
 unsigned char original[15];memcpy(original,entry,15);memcpy(trampoline,entry,15);sdk_absolute_jump(trampoline+15,entry+15);
 if(mprotect(trampoline,(size_t)page,PROT_READ|PROT_EXEC)){munmap(trampoline,(size_t)page);return 0;}
 uintptr_t start=(uintptr_t)entry&~((uintptr_t)page-1),end=((uintptr_t)entry+15+(uintptr_t)page-1)&~((uintptr_t)page-1);
 if(mprotect((void*)start,end-start,PROT_READ|PROT_WRITE|PROT_EXEC)){munmap(trampoline,(size_t)page);return 0;}
 sdk_original_image_dto=(sdk_image_dto_fn)trampoline;
 unsigned char patch[15];sdk_absolute_jump(patch,sdk_capture_image_dto);patch[14]=0x90;memcpy(entry,patch,sizeof(patch));__builtin___clear_cache((char*)entry,(char*)entry+15);
 if(mprotect((void*)start,end-start,PROT_READ|PROT_EXEC)){
  memcpy(entry,original,15);__builtin___clear_cache((char*)entry,(char*)entry+15);mprotect((void*)start,end-start,PROT_READ|PROT_EXEC);munmap(trampoline,(size_t)page);sdk_original_image_dto=NULL;return 0;
 }
 return 1; /* Mapped for the process lifetime; scope remains disabled until A. */
}
static int sdk_get_image_context(const char*md5,uint64_t generation,struct sdk_shared*out){
 int found=0;pthread_mutex_lock(&sdk_image_mutex);
 if(!atomic_load(&sdk_image_capture_enabled)||generation!=sdk_image_generation){pthread_mutex_unlock(&sdk_image_mutex);return 0;}
 for(unsigned i=0;i<16;i++)if(!strcmp(sdk_images[i].md5,md5)&&sdk_images[i].value.object){found=1;if(out){*out=sdk_images[i].value;sdk_shared_retain(out);}break;}
 pthread_mutex_unlock(&sdk_image_mutex);return found;
}
static int sdk_scope_target(const char*value){
 size_t n=strlen(value);if(!n||n>127)return 0;
 for(size_t i=0;i<n;i++)if(!((value[i]>='a'&&value[i]<='z')||(value[i]>='A'&&value[i]<='Z')||(value[i]>='0'&&value[i]<='9')||strchr("_.@-",value[i])))return 0;
 const char*at=strchr(value,'@');return !at||(at!=value&&!strcmp(at,"@chatroom"));
}
static void sdk_scope_success(uint64_t generation){
 pthread_mutex_lock(&sdk_mutex);snprintf(sdk_result,sizeof(sdk_result),"{\"success\":true,\"scope_generation\":%"PRIu64"}",generation);
 sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static void sdk_perform_image_scope(const char*payload,int voice_test,int multi){
 if(!sdk_download_hook_ready){sdk_finish_error("native image context capture unavailable");return;}
 if(!payload||strlen(payload)>8320){sdk_finish_error("invalid image scope");return;}
 const char*separator=strchr(payload,'\n');if(!separator||separator==payload||separator-payload>127){sdk_finish_error("invalid image scope");return;}
 char peers[64][128]={{0}},account[128]={0},current[128],name[512];unsigned count=0;
 if(multi){
  memcpy(account,payload,(size_t)(separator-payload));const char*start=separator+1;
  while(*start){const char*end=strchr(start,'\n');size_t n=end?(size_t)(end-start):strlen(start);
   if(!n||n>127||count==64){sdk_finish_error("invalid image targets");return;}
   memcpy(peers[count],start,n);if(!sdk_scope_target(peers[count])&&strcmp(peers[count],"*")){sdk_finish_error("invalid image target");return;}
   if(!strcmp(peers[count],"*")&&(count||end)){sdk_finish_error("invalid image target");return;}
   for(unsigned i=0;i<count;i++)if(!strcmp(peers[i],peers[count])){sdk_finish_error("duplicate image target");return;}
   count++;if(!end)break;start=end+1;if(!*start){sdk_finish_error("empty image target");return;}
  }
 }else{
  if(strlen(separator+1)>127||strchr(separator+1,'\n')){sdk_finish_error("invalid image scope");return;}
  memcpy(peers[0],payload,(size_t)(separator-payload));strcpy(account,separator+1);count=1;
  if(strncmp(peers[0],"wxid_",5)||!sdk_scope_target(peers[0])){sdk_finish_error("image scope requires account identifiers");return;}
 }
 if(!count||!sdk_scope_target(account)||strchr(account,'@')||!sdk_profile(current,name)||strcmp(current,account)){
  sdk_finish_error("image scope account is not logged in");return;
 }
 pthread_mutex_lock(&sdk_image_mutex);uint64_t generation=sdk_image_generation;
 int same=multi&&sdk_image_multi_scope&&atomic_load(&sdk_image_capture_enabled)&&!strcmp(account,sdk_image_account)&&count==sdk_image_peer_count;
 for(unsigned i=0;same&&i<count;i++){int found=0;for(unsigned j=0;j<count;j++)if(!strcmp(peers[i],sdk_image_peers[j]))found=1;if(!found)same=0;}
 pthread_mutex_unlock(&sdk_image_mutex);
 if(same){if(sdk_image_scope_current(generation))sdk_scope_success(generation);else sdk_finish_error("image scope account changed");return;}
 if(!sdk_clear_image_scope(generation,0)){sdk_finish_error("image scope superseded");return;}
 /* A switch during preparation must never arm a stale account. */
 if(!sdk_profile(current,name)||strcmp(current,account)){sdk_finish_error("image scope account changed");return;}
 pthread_mutex_lock(&sdk_image_mutex);
 if(sdk_image_generation!=generation+1){pthread_mutex_unlock(&sdk_image_mutex);sdk_finish_error("image scope superseded");return;}
 memcpy(sdk_image_peers,peers,sizeof(peers));strcpy(sdk_image_account,account);sdk_image_peer_count=count;sdk_image_multi_scope=multi;
 generation=sdk_image_generation;atomic_store(&sdk_image_capture_enabled,1);atomic_store(&sdk_voice_capture_enabled,voice_test);pthread_mutex_unlock(&sdk_image_mutex);
#ifdef SDK_FIXTURE
 void(*ready)(const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_image_scope_ready");if(ready)for(unsigned i=0;i<count;i++)ready(peers[i],account);
 if(voice_test){void(*voice_ready)(const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_voice_scope_ready");if(voice_ready)voice_ready(peers[0],account);}
#endif
 if(sdk_image_scope_current(generation))sdk_scope_success(generation);else sdk_finish_error("image scope account changed");
}
static int sdk_parse_generation(const char*value,size_t n,uint64_t*out){
 if(!n||n>20)return 0;
 uint64_t result=0;for(size_t i=0;i<n;i++){unsigned digit=(unsigned)(value[i]-'0');if(digit>9||result>(UINT64_MAX-digit)/10)return 0;result=result*10+digit;}
 if(!result)return 0;
 *out=result;return 1;
}
static int sdk_image_query(const char*payload,const char**md5,uint64_t*generation){
 const char*sep=payload?strchr(payload,'\n'):NULL;
 pthread_mutex_lock(&sdk_image_mutex);int multi=sdk_image_multi_scope;*generation=sdk_image_generation;pthread_mutex_unlock(&sdk_image_mutex);
 if(sep){if(!sdk_parse_generation(payload,(size_t)(sep-payload),generation))return 0;*md5=sep+1;}
 else {if(multi)return 0;*md5=payload;}
 return *md5&&sdk_md5_valid(*md5);
}
struct sdk_image_options {uint32_t mode,padding;unsigned char path_a[24],path_b[24];};
_Static_assert(sizeof(struct sdk_image_options)==56,"native image options ABI");
static void sdk_perform_message_download(const char*payload,int voice_test){
 const char*md5=NULL;uint64_t generation;
 pthread_mutex_lock(&sdk_image_mutex);generation=sdk_image_generation;pthread_mutex_unlock(&sdk_image_mutex);
 if(!sdk_download_hook_ready||(!voice_test&&!sdk_image_query(payload,&md5,&generation))){sdk_finish_error("native image context capture unavailable or invalid scope/checksum");return;}
 if(!sdk_image_scope_current(generation)){sdk_finish_error("image scope or account changed");return;}
 struct sdk_shared image={0};
 if(voice_test){pthread_mutex_lock(&sdk_image_mutex);if(atomic_load(&sdk_voice_capture_enabled)){image=sdk_voice_context;sdk_shared_retain(&image);}pthread_mutex_unlock(&sdk_image_mutex);}
 else sdk_get_image_context(md5,generation,&image);
 if(!image.object){sdk_finish_error("native media context not ready");return;}
 int kind=voice_test?11:3,scene=voice_test?0:100;
#ifdef SDK_FIXTURE
 int(*download)(const struct sdk_shared*,int,int,const void*)=(void*)dlsym(RTLD_DEFAULT,voice_test?"fixture_native_voice_download":"fixture_native_image_download");
 struct sdk_image_options options={.mode=1};int ok=download&&download(&image,kind,scene,&options)==1;sdk_release(&image);
 if(!ok){sdk_finish_error("fixture native image download rejected");return;}
#else
 struct sdk_shared hub={0},center={0},manager={0},future={0};struct sdk_image_options options={.mode=1};
 ((void(*)(void*))(sdk_base+0x4539510))(options.path_a);((void(*)(void*))(sdk_base+0x4539510))(options.path_b);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int ok=0;uint32_t errors[2]={0};
 if(manager.object){
  ((void(*)(struct sdk_shared*,void*,struct sdk_shared*,int,int,struct sdk_image_options*))(sdk_base+0x6517dc0))(&future,manager.object,&image,kind,scene,&options);
  if(future.object){
   ((void(*)(struct sdk_shared*))(sdk_base+0x9aef560))(&future);
   void*status=((void*(*)(struct sdk_shared*))(sdk_base+0x6545070))(&future);
   if(status&&rd(errors,status,8)&&!errors[0]&&!errors[1])ok=1;
  }
 }
 sdk_release(&future);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);sdk_release(&image);
 ((void(*)(void*))(sdk_base+0x4539690))(options.path_b);((void(*)(void*))(sdk_base+0x4539690))(options.path_a);
 if(!ok){char error[160];snprintf(error,sizeof(error),"native message image download failed: type=%u code=%d",errors[0],(int32_t)errors[1]);sdk_finish_error(error);return;}
#endif
 if(!sdk_image_scope_current(generation)){sdk_finish_error("image scope or account changed during download");return;}
 pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true,\"native_completed\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static void sdk_perform(char operation,const char *json_body){
 if((operation=='M'||operation=='O'||operation=='W'||operation=='R')&&!sdk_expected_account[0]){sdk_finish_error("bind the logged-in account before mention, emoticon, voice or video send");return;}
 if((operation=='S'||operation=='M'||operation=='G'||operation=='I'||operation=='F'||operation=='O'||operation=='W'||operation=='R')&&sdk_expected_account[0]){
  char account[128],name[512];if(!sdk_profile(account,name)||strcmp(account,sdk_expected_account)){
   sdk_finish_error("WeChat account changed; refusing stale send");return;
  }
 }

 if(operation!='V'&&!sdk_has_coroutine()){sdk_finish_error("native coroutine context unavailable; no send attempted");return;}
 if(operation=='Y'){sdk_perform_quote(json_body);return;}
 if(operation=='M'){sdk_perform_mention(json_body);return;}
 if(operation=='W'){sdk_perform_voice(json_body);return;}
 if(operation=='R'){sdk_perform_video(json_body);return;}
 if(operation=='T'){
  pthread_mutex_lock(&sdk_image_mutex);int available=atomic_load(&sdk_voice_capture_enabled)&&sdk_voice_context.object;pthread_mutex_unlock(&sdk_image_mutex);
  pthread_mutex_lock(&sdk_mutex);snprintf(sdk_result,sizeof(sdk_result),"{\"success\":true,\"available\":%s}",available?"true":"false");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);return;
 }
 if(operation=='A'||operation=='B'||operation=='C'){sdk_perform_image_scope(json_body,operation=='B',operation=='C');return;}
 if(operation=='K'){
  const char*md5=NULL;uint64_t generation;char result[200];
  if(!sdk_image_query(json_body,&md5,&generation)){sdk_finish_error("invalid image scope/checksum");return;}
  pthread_mutex_lock(&sdk_image_mutex);int active=atomic_load(&sdk_image_capture_enabled);pthread_mutex_unlock(&sdk_image_mutex);
  if((active||strchr(json_body,'\n'))&&!sdk_image_scope_current(generation)){sdk_finish_error("image scope or account changed");return;}
  int available=active&&sdk_get_image_context(md5,generation,NULL);
  if(active&&!sdk_image_scope_current(generation)){sdk_finish_error("image scope or account changed");return;}
  snprintf(result,sizeof(result),"{\"success\":true,\"available\":%s,\"scoped_seen\":%u,\"cached\":%u}",available?"true":"false",atomic_load(&sdk_image_scoped_seen),atomic_load(&sdk_image_cached_total));
  pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,result);sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);return;
 }
 if(operation=='D'||operation=='U'){sdk_perform_message_download(json_body,operation=='U');return;}
 if(operation=='I'||operation=='F'||operation=='O'){sdk_perform_media(operation,json_body);return;}
 if(operation=='N'){
  atomic_store(&sdk_context_verified,1);pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true,\"native_coroutine\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);return;
 }
 struct sdk_string json={0},request={0};
 const char*body=operation=='V'?"{}":json_body;
 sdk_str(&json,body,strlen(body));sdk_str(&request,"helper-linux-test",17);
 struct sdk_callback*cb=calloc(1,sizeof(*cb));
 if(!cb){sdk_string_destroy(&json);sdk_string_destroy(&request);sdk_finish_error("allocation failed");return;}
 atomic_init(&cb->refs,1);cb->invoke=sdk_cb_invoke;cb->destroy=sdk_cb_destroy;cb->check=sdk_cb_check;
 (operation=='G'?sdk_send_generic:sdk_send)(NULL,&json,&request,&cb);
 if(cb&&atomic_fetch_sub(&cb->refs,1)==1)cb->destroy(cb);
 sdk_string_destroy(&json);sdk_string_destroy(&request);
}
static void sdk_task_destroy(struct sdk_callable*t){if(t->payload&&atomic_fetch_sub(&t->payload->refs,1)==1){free(t->payload->json);free(t->payload);}t->payload=NULL;}
static void sdk_task_delete(struct sdk_callable*t){sdk_task_destroy(t);free(t);}
static struct sdk_callable*sdk_task_clone(struct sdk_callable*t){struct sdk_callable*n=malloc(sizeof(*n));if(n){*n=*t;if(n->payload)atomic_fetch_add(&n->payload->refs,1);}return n;}
static void sdk_task_clone_into(struct sdk_callable*t,void*dst){memcpy(dst,t,sizeof(*t));if(t->payload)atomic_fetch_add(&t->payload->refs,1);}
static void sdk_task_invoke(struct sdk_callable*t){sdk_perform(t->payload->operation,t->payload->json);}
static void*sdk_task_target(struct sdk_callable*t,const void*type){(void)t;(void)type;return NULL;}
static const void*sdk_task_type(struct sdk_callable*t){(void)t;return NULL;}
static void*sdk_task_vtable[]={sdk_task_destroy,sdk_task_delete,sdk_task_clone,sdk_task_clone_into,sdk_task_destroy,sdk_task_delete,sdk_task_invoke,sdk_task_target,sdk_task_type};
static void sdk_release(struct sdk_shared*s){
 if(s->control){int64_t*ref=(int64_t*)((char*)s->control+8);if(__atomic_fetch_sub(ref,1,__ATOMIC_SEQ_CST)==0){void**v=*(void***)s->control;((void(*)(void*))v[2])(s->control);
#ifdef SDK_FIXTURE
 void(*weak)(void*)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_weak_release");if(weak)weak(s->control);
#else
 ((void(*)(void*))(sdk_base+0x45391d0))(s->control);
#endif
}}memset(s,0,sizeof(*s));
}
static void sdk_schedule(char operation,const char *json){
 struct sdk_callable*t=calloc(1,sizeof(*t));if(!t){sdk_finish_error("allocation failed");return;}t->vtable=sdk_task_vtable;t->payload=calloc(1,sizeof(*t->payload));
 if(!t->payload){free(t);sdk_finish_error("allocation failed");return;}
 atomic_init(&t->payload->refs,1);t->payload->operation=operation;t->payload->json=strdup(json?json:"{}");
 if(!t->payload->json){free(t->payload);free(t);sdk_finish_error("allocation failed");return;}
 struct sdk_function fn={0};fn.callable=t;
 struct sdk_source source={"helper_linux_native.c","sdk_schedule",1,0,sdk_schedule};
 struct sdk_shared executor={0},job={0};(void)executor;
#ifdef SDK_FIXTURE
 void(*dispatch)(struct sdk_shared*,void*,const struct sdk_source*,struct sdk_function*,int)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_dispatch");
 if(!dispatch){sdk_task_delete(t);sdk_finish_error("fixture dispatcher missing");return;}dispatch(&job,NULL,&source,&fn,1);
#else
 void*app=((void*(*)(void))(sdk_base+0x6041320))();
 if(!app){sdk_task_delete(t);sdk_finish_error("native app unavailable");return;}
 ((void(*)(struct sdk_shared*,void*))(sdk_base+0x60422b0))(&executor,app);
 void*queue=NULL;if(!executor.object||!rd(&queue,(char*)executor.object+0x10,8)||!queue){sdk_task_delete(t);sdk_release(&executor);sdk_finish_error("native dispatcher unavailable");return;}
 ((void(*)(struct sdk_shared*,void*,const struct sdk_source*,struct sdk_function*,int))(sdk_base+0x6247520))(&job,executor.object,&source,&fn,1);
 sdk_release(&job);sdk_release(&executor);
#endif
 if(fn.callable){struct sdk_callable*c=fn.callable;((void(*)(void*))c->vtable[5])(c);}
}
static void sdk_handle(int client,char operation,const char *json){
 if(operation=='Y'&&(!sdk_quote_ready||!json)){sdk_write(client,"{\"error\":\"native quote unavailable\"}\n");return;}
 if(operation!='Y'&&operation!='V'&&operation!='W'&&operation!='R'&&operation!='M'&&operation!='S'&&operation!='N'&&operation!='G'&&operation!='I'&&operation!='F'&&operation!='O'&&operation!='D'&&operation!='A'&&operation!='B'&&operation!='C'&&operation!='T'&&operation!='U'&&operation!='K'){sdk_write(client,"{\"error\":\"unsupported command\"}\n");return;}
 if((operation=='Y'||operation=='S'||operation=='M'||operation=='G'||operation=='I'||operation=='F'||operation=='O'||operation=='W'||operation=='R'||operation=='D'||operation=='U')&&!atomic_load(&sdk_context_verified)){sdk_write(client,"{\"error\":\"native context has not passed the dry run\"}\n");return;}
 pthread_mutex_lock(&sdk_mutex);
 /* A timed-out caller does not retry; a completed late callback must not block later work. */
 if(sdk_running&&sdk_done)sdk_running=0;
 if(sdk_running){pthread_mutex_unlock(&sdk_mutex);sdk_write(client,"{\"error\":\"previous callback pending\"}\n");return;}
 sdk_done=0;sdk_running=1;sdk_result[0]=0;sdk_error[0]=0;pthread_mutex_unlock(&sdk_mutex);
 sdk_schedule(operation,json);
 pthread_mutex_lock(&sdk_mutex);
 struct timespec until;clock_gettime(CLOCK_REALTIME,&until);
#ifdef HELPER_FIXTURE_WAIT_MS
 until.tv_sec+=HELPER_FIXTURE_WAIT_MS/1000;until.tv_nsec+=(HELPER_FIXTURE_WAIT_MS%1000)*1000000L;
 if(until.tv_nsec>=1000000000L){until.tv_sec++;until.tv_nsec-=1000000000L;}
#else
 until.tv_sec+=45;
#endif
 while(!sdk_done){int rc=pthread_cond_timedwait(&sdk_cond,&sdk_mutex,&until);if(rc==ETIMEDOUT)break;}
 char result[50000];if(sdk_done){char a[32768],b[8192];sdk_escape(a,sizeof(a),sdk_result);sdk_escape(b,sizeof(b),sdk_error);snprintf(result,sizeof(result),"{\"callback\":true,\"result\":\"%s\",\"error\":\"%s\"}\n",a,b);sdk_running=0;}else snprintf(result,sizeof(result),"{\"callback\":false,\"pending\":true}\n");
 pthread_mutex_unlock(&sdk_mutex);sdk_write(client,result);
}
static int sdk_read(int fd,void*out,size_t n){unsigned char*p=out;while(n){ssize_t k=recv(fd,p,n,0);if(k<=0){if(k<0&&errno==EINTR)continue;return 0;}p+=k;n-=(size_t)k;}return 1;}
static void*sdk_serve(void*unused){
 (void)unused;
 while(getppid()==sdk_parent){
  fd_set set;FD_ZERO(&set);FD_SET(sdk_listener,&set);struct timeval tv={1,0};
  int rc=select(sdk_listener+1,&set,NULL,NULL,&tv);if(rc<0){if(errno==EINTR)continue;break;}if(!rc)continue;
  int c=accept4(sdk_listener,NULL,NULL,SOCK_CLOEXEC);if(c<0)continue;
  struct ucred peer={0};socklen_t size=sizeof(peer);
  if(getsockopt(c,SOL_SOCKET,SO_PEERCRED,&peer,&size)||peer.pid!=sdk_parent||peer.uid!=geteuid()){close(c);continue;}
  struct timeval timeout={5,0};setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
  char op=0;if(recv(c,&op,1,0)==1){
   if(op=='Q'){sdk_clear_image_scope(0,1);sdk_write(c,"{\"stopped\":true}\n");close(c);break;}
   if(op=='P'){
    char account[128],name[512],a[1024],n[4096],response[5500];int logged=sdk_profile(account,name);
    sdk_escape(a,sizeof(a),logged?account:"");sdk_escape(n,sizeof(n),logged?name:"");
    snprintf(response,sizeof(response),"{\"logged_in\":%s,\"wxid\":\"%s\",\"name\":\"%s\"}\n",logged?"true":"false",a,n);sdk_write(c,response);
   }else if(op=='H'){char ready[512];snprintf(ready,sizeof(ready),"{\"ready\":true,\"protocol\":1,\"generic_messages\":true,\"group_mentions\":true,\"local_media\":true,\"local_emoticons\":true,\"local_voice\":true,\"local_video\":true,\"image_download\":%s,\"image_context\":%s,\"image_scope_groups\":%s,\"native_quote\":%s}\n",sdk_download_hook_ready?"true":"false",sdk_download_hook_ready?"true":"false",sdk_download_hook_ready?"true":"false",sdk_quote_ready?"true":"false");sdk_write(c,ready);}
   else if(op=='Y'||op=='S'||op=='M'||op=='G'||op=='I'||op=='F'||op=='O'||op=='W'||op=='R'||op=='D'||op=='A'||op=='B'||op=='C'||op=='X'||op=='K'||op=='E'){
    uint32_t net=0;char*body=NULL;
    if(sdk_read(c,&net,4)){size_t n=ntohl(net);if(n&&(op!='Y'||n<=1193)&&(op!='W'||n<=2181)&&(op!='R'||n<=2300)){body=malloc(n+1);if(body&&sdk_read(c,body,n)){body[n]=0;if(!memchr(body,0,n)){
     if(op=='X'){
      uint64_t generation=0;
      int cleared=sdk_parse_generation(body,n,&generation)&&sdk_clear_image_scope(generation,0);
      sdk_write(c,cleared?"{\"callback\":true,\"result\":\"{\\\"success\\\":true,\\\"cleared\\\":true}\"}\n":"{\"callback\":true,\"result\":\"{\\\"success\\\":true,\\\"cleared\\\":false}\"}\n");
     }else if(op=='E'){
      char account[128],name[512];
      if(n<128&&sdk_profile(account,name)&&!strcmp(account,body)){
       pthread_mutex_lock(&sdk_mutex);int busy=sdk_running&&!sdk_done;
       if(!busy)memcpy(sdk_expected_account,body,n+1);
       pthread_mutex_unlock(&sdk_mutex);
       if(busy)sdk_write(c,"{\"error\":\"previous callback pending\"}\n");else sdk_write(c,"{\"callback\":true,\"result\":\"{\\\"success\\\":true}\",\"error\":\"\"}\n");
      }else sdk_write(c,"{\"error\":\"account is not logged in\"}\n");
     }else sdk_handle(c,op,body);
    }else sdk_write(c,"{\"error\":\"NUL in JSON request\"}\n");}}}
    if(!body)sdk_write(c,"{\"error\":\"invalid request frame\"}\n");
    free(body);
   }else sdk_handle(c,op,NULL);
  }close(c);
 }
 sdk_clear_image_scope(0,1);
 close(sdk_listener);sdk_listener=-1;unlink(sdk_path);return NULL;
}
__attribute__((constructor))static void sdk_init(void){
 const char*enabled=getenv("HELPER_LINUX_SEND_BRIDGE");if(!enabled||strcmp(enabled,"1"))return;
 const char*pidstr=getenv("HELPER_LINUX_CONTROLLER_PID"),*path=getenv("HELPER_LINUX_SEND_SOCKET");char*end=NULL;
 if(!pidstr||!path||path[0]!='/'||strlen(path)>=sizeof(sdk_path))return;
 errno=0;long parent=strtol(pidstr,&end,10);if(errno||!end||*end||parent<=0||parent>INT32_MAX||getppid()!=(pid_t)parent)return;sdk_parent=(pid_t)parent;
#ifdef SDK_FIXTURE
 sdk_send=(sdk_entry)dlsym(RTLD_DEFAULT,"fixture_sdk_send");sdk_send_generic=(sdk_entry)dlsym(RTLD_DEFAULT,"fixture_sdk_send_generic");if(!sdk_send_generic)sdk_send_generic=sdk_send;sdk_str=(sdk_ctor)dlsym(RTLD_DEFAULT,"fixture_sdk_string");sdk_delete=(void(*)(void*))dlsym(RTLD_DEFAULT,"fixture_sdk_delete");
 if(!sdk_send||!sdk_str||!sdk_delete)return;
#else
 if(!check_hash())return;
 uintptr_t base=0;dl_iterate_phdr(main_build,&base);if(!base)return;
 static const unsigned char expected[]={0x41,0x57,0x41,0x56,0x41,0x54,0x53,0x48,0x81,0xec,0x98,0,0,0,0x48,0x89,0xcb};
 if(memcmp((void*)(base+0x6f19410),expected,sizeof(expected)))return;
 sdk_base=base;
 sdk_send=(sdk_entry)(base+0x6f19410);sdk_send_generic=(sdk_entry)(base+0x6f18b40);sdk_str=(sdk_ctor)(base+0x450e6a0);sdk_delete=(void(*)(void*))(base+0x450c140);
#endif
 char parentdir[108];strcpy(parentdir,path);char*slash=strrchr(parentdir,'/');if(!slash||slash==parentdir)return;*slash=0;
 struct stat st;if(lstat(parentdir,&st)||!S_ISDIR(st.st_mode)||st.st_uid!=geteuid()||(st.st_mode&0777)!=0700)return;
 sdk_listener=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(sdk_listener<0)return;
 struct sockaddr_un sa={.sun_family=AF_UNIX};strcpy(sa.sun_path,path);strcpy(sdk_path,path);
 if(bind(sdk_listener,(struct sockaddr*)&sa,sizeof(sa))||chmod(path,0600)||listen(sdk_listener,4)){close(sdk_listener);sdk_listener=-1;return;}
 sdk_download_hook_ready=sdk_install_image_context_capture();
#ifndef SDK_FIXTURE
 sdk_quote_ready=sdk_download_hook_ready;
#endif
 pthread_t worker;if(pthread_create(&worker,NULL,sdk_serve,NULL)){close(sdk_listener);sdk_listener=-1;unlink(path);return;}pthread_detach(worker);
}
